"""Credential-free checks for bounded `allow` conditions: they only ever narrow a signed rule."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


policy = _load("policy_conditions_under_test", "platform/core/policy.py")
govern = _load("govern_action_for_conditions", "scripts/govern-action.py")
compiler = _load("compile_workspace_for_conditions", "scripts/compile-workspace.py")

FAQ_ONLY = {"arguments": {"category": ["faq"]}}


def signed(rule: dict, owner: str = "owner@client.example") -> dict:
    return {**rule, "signoff": {"owner": owner, "ruleDigest": policy.rule_digest(rule)}}


def doc(*rules: dict) -> dict:
    return {
        "apiVersion": "modelgarden.ai/v1", "kind": "Policy",
        "metadata": {"name": "client-policy", "owner": "client"}, "spec": {"rules": list(rules)},
    }


def evaluate(*rules: dict, tool: str = "mail.send", risk: str = "external-communication", **kwargs) -> dict:
    return policy.evaluate([doc(*rules)], tool_name=tool, risk=risk, tool_requires_approval=True, **kwargs)


def rule(**conditions) -> dict:
    return signed({"id": "faq-replies", "effect": "allow", "actions": ["mail.send"], "conditions": conditions})


class ArgumentConditionTests(unittest.TestCase):
    def test_signed_rule_applies_only_when_every_named_argument_matches(self):
        faq = rule(**FAQ_ONLY)
        self.assertEqual(evaluate(faq, arguments={"category": "faq"})["decision"], "allow")
        # Wrong value, missing argument, wrong type and no arguments at all keep the stricter default.
        for arguments in ({"category": "billing"}, {}, {"category": ["faq"]}, {"category": None}, None):
            result = evaluate(faq, arguments=arguments)
            self.assertEqual((result["decision"], result["reason"]), ("require-approval", "tool-requires-approval"), arguments)

    def test_values_are_compared_by_type_so_true_does_not_match_one(self):
        flag = signed({"id": "flag", "effect": "allow", "actions": ["mail.send"],
                       "conditions": {"arguments": {"urgent": [1]}}})
        self.assertEqual(evaluate(flag, arguments={"urgent": 1})["decision"], "allow")
        self.assertEqual(evaluate(flag, arguments={"urgent": True})["decision"], "require-approval")

    def test_conditions_are_bound_by_the_owner_signoff(self):
        faq = rule(**FAQ_ONLY)
        widened = {**faq, "conditions": {"arguments": {"category": ["faq", "billing"]}}}
        self.assertFalse(policy.signoff_valid(widened))
        self.assertEqual(evaluate(widened, arguments={"category": "billing"})["decision"], "require-approval")

    def test_conditions_cannot_loosen_a_restrictive_rule_and_are_rejected_on_one(self):
        tools = [{"metadata": {"name": "mail.send"}, "spec": {"risk": "external-communication"}}]
        deny = {"id": "no-mail", "effect": "deny", "actions": ["mail.send"], "conditions": FAQ_ONLY}
        errors = policy.relaxation_errors([doc(deny)], tools)
        self.assertTrue(any("no-mail" in e and "not an allow rule" in e for e in errors), errors)
        # Even if one slips through, the restrictive rule is never narrowed by its own conditions.
        self.assertEqual(evaluate(deny, arguments={"category": "billing"})["decision"], "deny")

    def test_an_unsigned_conditional_allow_still_needs_the_owner_signoff(self):
        tools = [{"metadata": {"name": "mail.send"}, "spec": {"risk": "external-communication"}}]
        unsigned = {"id": "faq", "effect": "allow", "actions": ["mail.send"], "conditions": FAQ_ONLY}
        self.assertTrue(any("sign-off" in e for e in policy.relaxation_errors([doc(unsigned)], tools)))
        self.assertEqual(evaluate(unsigned, arguments={"category": "faq"})["decision"], "require-approval")

    def test_a_public_principal_cannot_use_a_conditional_rule_that_names_members_only(self):
        faq = rule(**FAQ_ONLY)
        result = evaluate(faq, arguments={"category": "faq"}, principal="public")
        self.assertEqual(result["decision"], "deny")
        # A signed public write rule with an unmet condition grants nothing.
        key = policy.generate_owner_key()
        booking = policy.sign_rule({"id": "public-book", "effect": "allow", "actions": ["calendar.book"], "principals": ["public"],
                                    "conditions": {"arguments": {"service": ["haircut"]}}},
                                   "owner@client.example", key["private"])
        kwargs = dict(tool="calendar.book", risk="write", principal="public",
                      owner_keys={"owner@client.example": key["public"]})
        self.assertEqual(evaluate(booking, arguments={"service": "haircut"}, **kwargs)["decision"], "allow")
        self.assertEqual(evaluate(booking, arguments={"service": "surgery"}, **kwargs)["decision"], "deny")


class UsageLimitTests(unittest.TestCase):
    def test_rule_applies_below_the_daily_limit_and_stops_at_it(self):
        limited = rule(maxPerDay=2)
        self.assertEqual(evaluate(limited, usage_today=1)["decision"], "allow")
        reached = evaluate(limited, usage_today=2)
        self.assertEqual((reached["decision"], reached["reason"], reached["ruleId"]),
                         ("require-approval", "policy-limit-reached", "faq-replies"))

    def test_an_unprovable_count_never_lets_the_rule_apply(self):
        result = evaluate(rule(maxPerDay=5), usage_today=None)
        self.assertEqual((result["decision"], result["reason"]), ("require-approval", "tool-requires-approval"))

    def test_both_conditions_must_hold(self):
        both = rule(arguments=FAQ_ONLY["arguments"], maxPerDay=3)
        self.assertEqual(evaluate(both, arguments={"category": "faq"}, usage_today=0)["decision"], "allow")
        self.assertEqual(evaluate(both, arguments={"category": "faq"}, usage_today=3)["decision"], "require-approval")
        self.assertEqual(evaluate(both, arguments={"category": "x"}, usage_today=0)["decision"], "require-approval")

    def test_preview_reports_conditional_autonomy_instead_of_claiming_it_is_automatic(self):
        preview = policy.preview([doc(rule(maxPerDay=2))], tool_name="mail.send", risk="external-communication",
                                 tool_requires_approval=True)
        self.assertEqual(preview, {"member": "conditional", "public": "denied"})
        unconditional = signed({"id": "all", "effect": "allow", "actions": ["mail.send"]})
        self.assertEqual(policy.preview([doc(unconditional)], tool_name="mail.send", risk="external-communication",
                                        tool_requires_approval=True)["member"], "automatic")


class LedgerCountTests(unittest.TestCase):
    TOOLS = [{"metadata": {"name": "mail.send"}, "spec": {"type": "internal", "risk": "external-communication",
                                                         "approvalRequired": True}}]

    def state(self, *rules: dict) -> dict:
        return {
            "schemaVersion": 1, "source": {"name": "support"},
            "agent": {"spec": {"approvals": {"externalActions": "none"}, "security": {"dataClassification": "internal"}}},
            "tools": self.TOOLS, "policies": [doc(*rules)],
        }

    def run_send(self, state: dict, audit: pathlib.Path, category: str = "faq") -> dict:
        return govern.execute_action(
            state, "mail.send", {"to": "a@example.com", "category": category}, lambda args: {"id": "m"}, audit_path=audit,
        )

    def test_daily_limit_is_enforced_from_the_local_ledger_and_ledgered_with_the_rule(self):
        state = self.state(rule(maxPerDay=2, **FAQ_ONLY))
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            first = self.run_send(state, audit)
            second = govern.execute_action(
                state, "mail.send", {"to": "b@example.com", "category": "faq"}, lambda args: {"id": "m"}, audit_path=audit)
            self.assertEqual((first["status"], second["status"]), ("executed", "executed"))
            third = govern.execute_action(
                state, "mail.send", {"to": "c@example.com", "category": "faq"}, lambda args: {"id": "m"}, audit_path=audit)
            self.assertEqual(third["status"], "pending-approval")
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            last = [e for e in events if e["event"] == "authorization"][-1]
            self.assertEqual(last["details"]["reason"], "policy-limit-reached")
            self.assertEqual(last["ledger"]["policyRuleId"], "faq-replies")
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])

    def test_another_category_is_not_automatic_even_with_headroom(self):
        state = self.state(rule(maxPerDay=5, **FAQ_ONLY))
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self.assertEqual(self.run_send(state, audit, "billing")["status"], "pending-approval")

    def test_without_a_ledger_a_limited_rule_does_not_apply(self):
        state = self.state(rule(maxPerDay=5))
        outcome = govern.authorize_action(state, "mail.send", {"to": "a@example.com"})
        self.assertEqual((outcome["decision"], outcome["reason"]), ("require-approval", "tool-requires-approval"))

    def test_count_ignores_other_tools_failures_other_days_and_fails_closed_on_corruption(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)

        def line(tool="mail.send", outcome="succeeded", stamp="2026-10-15T01:00:00Z", event="execution"):
            return json.dumps({"event": event, "outcome": outcome, "tool": tool, "timestamp": stamp})

        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self.assertEqual(govern.count_executions_today(audit, "mail.send", now=now), 0)
            audit.write_text("\n".join([
                line(), line(), line(tool="calendar.book"), line(outcome="failed"),
                line(stamp="2026-10-14T23:59:59Z"), line(event="authorization"),
            ]) + "\n", encoding="utf-8")
            self.assertEqual(govern.count_executions_today(audit, "mail.send", now=now), 2)
            audit.write_text(line() + "\nnot json\n", encoding="utf-8")
            self.assertIsNone(govern.count_executions_today(audit, "mail.send", now=now))
        self.assertIsNone(govern.count_executions_today(None, "mail.send", now=now))


class CompiledConditionTests(unittest.TestCase):
    def workspace(self, tmp: str, *extra_rules: dict) -> pathlib.Path:
        target = pathlib.Path(tmp) / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", target)
        agent_file = target / "agents/front-office/receptionist/agent.yaml"
        agent = yaml.safe_load(agent_file.read_text())
        agent["spec"]["disabledSkills"] = []
        agent_file.write_text(yaml.safe_dump(agent))
        path = target / "policies" / "default.yaml"
        document = yaml.safe_load(path.read_text())
        document["spec"]["rules"].extend(extra_rules)
        path.write_text(yaml.safe_dump(document))
        return target

    def test_schema_accepts_a_bounded_signed_rule_and_rejects_malformed_conditions(self):
        good = rule(maxPerDay=3, **FAQ_ONLY)
        good = {**good, "id": "faq-book", "actions": ["calendar.book"]}
        good["signoff"] = {"owner": "owner@client.example", "ruleDigest": policy.rule_digest(good)}
        with tempfile.TemporaryDirectory() as tmp:
            compiled = compiler.compile_workspace(self.workspace(tmp, good), "receptionist")[0]
        self.assertTrue(any(r["id"] == "faq-book" for p in compiled["policies"] for r in p["spec"]["rules"]))
        for bad in ({"maxPerDay": 0}, {"maxPerDay": "3"}, {"arguments": {}}, {"arguments": {"c": []}},
                    {"arguments": {"c": [{"x": 1}]}}, {"unknown": 1}, {}):
            malformed = {"id": "bad", "effect": "allow", "actions": ["calendar.book"], "conditions": bad}
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError, msg=str(bad)):
                compiler.compile_workspace(self.workspace(tmp, malformed), "receptionist")

    def test_editing_a_condition_after_signing_blocks_compilation_until_resigned(self):
        original = {"id": "faq-book", "effect": "allow", "actions": ["calendar.book"], "conditions": FAQ_ONLY}
        approved = signed(original)
        widened = {**approved, "conditions": {"arguments": {"category": ["faq", "billing"]}}}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(compiler.compile_workspace(self.workspace(tmp, approved), "receptionist"))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "faq-book.*sign-off"):
            compiler.compile_workspace(self.workspace(tmp, widened), "receptionist")

    def test_approval_preview_shows_conditional_for_a_conditioned_allow_rule(self):
        booking = signed({"id": "limited-book", "effect": "allow", "actions": ["calendar.book"],
                          "conditions": {"maxPerDay": 4}})
        with tempfile.TemporaryDirectory() as tmp:
            report = compiler.workspace_approvals(self.workspace(tmp, booking), "receptionist", ())
            text = compiler.format_approvals(report)
        rows = {a["tool"]: a for entry in report[0]["skills"] for a in entry["actions"]}
        self.assertEqual(rows["calendar.book"]["member"], "conditional")
        self.assertIn("conditional", text)


if __name__ == "__main__":
    unittest.main()
