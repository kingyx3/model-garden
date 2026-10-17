"""Credential-free checks for the per-requester daily limit and per-Agent usage counts on bounded `allow` rules (#114, #95, #129)."""
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


policy = _load("policy_for_principal_limits", "platform/core/policy.py")
govern = _load("govern_for_principal_limits", "scripts/govern-action.py")
compiler = _load("compiler_for_principal_limits", "scripts/compile-workspace.py")

ALICE, BOB = "alice@acme.example", "bob@acme.example"
STRANGER = "unmapped:slack:3f2a9c0d11"


def signed(rule: dict, owner: str = "owner@client.example") -> dict:
    return {**rule, "signoff": {"owner": owner, "ruleDigest": policy.rule_digest(rule)}}


def doc(*rules: dict) -> dict:
    return {"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": "client-policy", "owner": "client"},
            "spec": {"rules": list(rules)}}


def rule(**conditions) -> dict:
    return signed({"id": "faq-replies", "effect": "allow", "actions": ["mail.send"], "conditions": conditions})


def evaluate(*rules: dict, **kwargs) -> dict:
    return policy.evaluate([doc(*rules)], tool_name="mail.send", risk="external-communication", tool_requires_approval=True, **kwargs)


class EvaluateTests(unittest.TestCase):
    def test_a_requester_below_their_limit_is_automatic_and_at_it_is_not(self):
        limited = rule(maxPerPrincipalPerDay=2)
        self.assertEqual(evaluate(limited, usage_by_principal=1)["decision"], "allow")
        reached = evaluate(limited, usage_by_principal=2)
        self.assertEqual((reached["decision"], reached["reason"], reached["ruleId"]), ("require-approval", "policy-limit-reached", "faq-replies"))

    def test_an_unattributed_requester_has_no_count_so_the_rule_does_not_apply(self):
        result = evaluate(rule(maxPerPrincipalPerDay=5), usage_by_principal=None, usage_today=0)
        self.assertEqual((result["decision"], result["reason"]), ("require-approval", "tool-requires-approval"))  # not "limit reached": nothing was proven

    def test_both_limits_must_hold(self):
        both = rule(maxPerDay=10, maxPerPrincipalPerDay=2)
        self.assertEqual(evaluate(both, usage_today=5, usage_by_principal=1)["decision"], "allow")
        self.assertEqual(evaluate(both, usage_today=10, usage_by_principal=0)["decision"], "require-approval")  # the Tool-wide cap
        self.assertEqual(evaluate(both, usage_today=5, usage_by_principal=2)["decision"], "require-approval")  # this person's cap
        self.assertEqual(evaluate(both, usage_today=None, usage_by_principal=0)["decision"], "require-approval")

    def test_the_new_limit_combines_with_an_argument_condition(self):
        both = rule(arguments={"category": ["faq"]}, maxPerPrincipalPerDay=3)
        self.assertEqual(evaluate(both, arguments={"category": "faq"}, usage_by_principal=0)["decision"], "allow")
        self.assertEqual(evaluate(both, arguments={"category": "billing"}, usage_by_principal=0)["decision"], "require-approval")

    def test_it_can_only_narrow_never_loosen(self):
        limited = rule(maxPerPrincipalPerDay=2)
        tools = [{"metadata": {"name": "mail.send"}, "spec": {"risk": "external-communication"}}]
        deny = {"id": "no-mail", "effect": "deny", "actions": ["mail.send"], "conditions": {"maxPerPrincipalPerDay": 2}}
        self.assertTrue(any("not an allow rule" in error for error in policy.relaxation_errors([doc(deny)], tools)))
        self.assertEqual(evaluate(deny, usage_by_principal=99)["decision"], "deny")
        self.assertEqual(evaluate(limited, usage_by_principal=99, agent_requires_approval=True)["reason"], "agent-requires-approval")

    def test_the_owner_signoff_binds_the_new_limit(self):
        original = rule(maxPerPrincipalPerDay=2)
        widened = {**original, "conditions": {"maxPerPrincipalPerDay": 200}}
        self.assertFalse(policy.signoff_valid(widened))
        self.assertEqual(evaluate(widened, usage_by_principal=50)["decision"], "require-approval")

    def test_the_approval_preview_reports_conditional_for_a_rule_with_only_the_new_limit(self):
        preview = policy.preview([doc(rule(maxPerPrincipalPerDay=1))], tool_name="mail.send", risk="external-communication", tool_requires_approval=True)
        self.assertEqual(preview["member"], "conditional")


class LedgerTests(unittest.TestCase):
    TOOLS = [{"metadata": {"name": "mail.send"}, "spec": {"type": "internal", "risk": "external-communication", "approvalRequired": True}}]

    def state(self, *rules: dict, name: str = "support") -> dict:
        return {"schemaVersion": 1, "source": {"name": name}, "agent": {"metadata": {"name": name},
                "spec": {"approvals": {"externalActions": "none"}, "security": {"dataClassification": "internal"}}},
                "tools": self.TOOLS, "policies": [doc(*rules)]}

    def send(self, state, audit, who, *, access="member"):
        return govern.execute_action(state, "mail.send", {"to": "x@example.com"}, lambda args: {"id": "m"}, audit_path=audit,
                                     initiating_user=who, principal_access=access)

    def test_each_requester_has_their_own_allowance_and_one_cannot_use_up_anothers(self):
        state = self.state(rule(maxPerPrincipalPerDay=2))
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self.assertEqual([self.send(state, audit, ALICE)["status"] for _ in range(2)], ["executed", "executed"])
            self.assertEqual(self.send(state, audit, ALICE)["status"], "pending-approval")  # Alice is out of allowance
            self.assertEqual(self.send(state, audit, BOB)["status"], "executed")  # Bob's is untouched
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            blocked = [e for e in events if e["event"] == "authorization" and e["details"].get("reason") == "policy-limit-reached"]
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0]["ledger"]["policyRuleId"], "faq-replies")
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])

    def test_the_tool_wide_limit_still_caps_everyone_together(self):
        state = self.state(rule(maxPerDay=2, maxPerPrincipalPerDay=5))
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self.assertEqual([self.send(state, audit, who)["status"] for who in (ALICE, BOB)], ["executed", "executed"])
            self.assertEqual(self.send(state, audit, "carol@acme.example")["status"], "pending-approval")

    def test_an_unattributed_call_never_gets_the_per_person_rule(self):
        state = self.state(rule(maxPerPrincipalPerDay=5))
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self.assertEqual(self.send(state, audit, None)["status"], "pending-approval")
            outcome = govern.authorize_action(state, "mail.send", {"to": "x@example.com"}, audit_path=audit)
            self.assertEqual(outcome["reason"], "tool-requires-approval")

    def test_a_public_sender_is_limited_on_their_own_and_falls_back_to_the_floor(self):
        public_rule = signed({"id": "public-faq", "effect": "allow", "actions": ["mail.send"], "principals": ["public"],
                              "conditions": {"maxPerPrincipalPerDay": 1}})
        # mail.send is external-communication: a public principal never reaches it whatever the rule says.
        denied = govern.authorize_action(self.state(public_rule), "mail.send", {"to": "x@example.com"}, initiating_user=STRANGER, principal_access="public")
        self.assertEqual((denied["decision"], denied["reason"]), ("deny", "principal-not-authorized"))
        write_tool = [{"metadata": {"name": "calendar.book"}, "spec": {"type": "internal", "risk": "write", "approvalRequired": True}}]
        book = signed({"id": "public-book", "effect": "allow", "actions": ["calendar.book"], "principals": ["public"],
                       "conditions": {"maxPerPrincipalPerDay": 1}})
        booking = {**self.state(book), "tools": write_tool}
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"

            def book_for(who):
                return govern.execute_action(booking, "calendar.book", {"slot": "10:00"}, lambda args: {"id": "b"}, audit_path=audit,
                                             initiating_user=who, principal_access="public")["status"]

            self.assertEqual(book_for(STRANGER), "executed")  # the rule names public, so the first booking is allowed
            with self.assertRaises(PermissionError):  # past their allowance a public sender falls back to the floor: refused, not queued
                book_for(STRANGER)
            self.assertEqual(book_for("unmapped:slack:aaaaaaaaaa"), "executed")  # a different sender has their own count

    def test_counts_are_per_agent_in_a_shared_ledger(self):
        support, sales = self.state(rule(maxPerDay=2), name="support"), self.state(rule(maxPerDay=2), name="sales")
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self.assertEqual([self.send(sales, audit, ALICE)["status"] for _ in range(2)], ["executed", "executed"])
            self.assertEqual(self.send(sales, audit, ALICE)["status"], "pending-approval")
            self.assertEqual(self.send(support, audit, ALICE)["status"], "executed")  # sales' use did not ration support
            self.assertEqual(govern.count_executions_today(audit, "mail.send", agent="sales"), 2)
            self.assertEqual(govern.count_executions_today(audit, "mail.send", agent="support"), 1)
            self.assertEqual(govern.count_executions_today(audit, "mail.send"), 3)

    def test_counting_filters_by_requester_and_treats_events_without_an_agent_as_counting(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)

        def line(**extra):
            return json.dumps({"event": "execution", "outcome": "succeeded", "tool": "mail.send", "timestamp": "2026-10-15T01:00:00Z", **extra})

        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            audit.write_text("\n".join([line(agent="support", initiatingUser=ALICE), line(agent="support", initiatingUser=BOB),
                                        line(agent="sales", initiatingUser=ALICE), line(initiatingUser=ALICE), line(agent="support")]) + "\n")
            count = lambda **kw: govern.count_executions_today(audit, "mail.send", now=now, **kw)
            self.assertEqual(count(), 5)
            self.assertEqual(count(agent="support"), 4)  # support's three plus the one that names no Agent
            self.assertEqual(count(agent="support", initiating_user=ALICE), 2)
            self.assertEqual(count(initiating_user=BOB), 1)
            self.assertEqual(count(initiating_user="nobody@acme.example"), 0)


class CompileTests(unittest.TestCase):
    def workspace(self, tmp: str, rule_: dict) -> pathlib.Path:
        target = pathlib.Path(tmp) / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", target)
        agent_file = target / "agents/front-office/receptionist/agent.yaml"
        agent = yaml.safe_load(agent_file.read_text())
        agent["spec"]["disabledSkills"] = []  # booking enabled, so calendar.book is selected and a relaxing rule is checked
        agent_file.write_text(yaml.safe_dump(agent))
        path = target / "policies" / "default.yaml"
        document = yaml.safe_load(path.read_text())
        document["spec"]["rules"].append(rule_)
        path.write_text(yaml.safe_dump(document))
        return target

    def test_schema_accepts_the_limit_and_rejects_malformed_values(self):
        good = signed({"id": "faq-book", "effect": "allow", "actions": ["calendar.book"], "conditions": {"maxPerPrincipalPerDay": 2}})
        with tempfile.TemporaryDirectory() as tmp:
            compiled = compiler.compile_workspace(self.workspace(tmp, good), "receptionist")[0]
        self.assertTrue(any(r["id"] == "faq-book" for p in compiled["policies"] for r in p["spec"]["rules"]))
        for bad in (0, "2", 10001, -1, 1.5, True):
            malformed = {"id": "bad", "effect": "allow", "actions": ["calendar.book"], "conditions": {"maxPerPrincipalPerDay": bad}}
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError, msg=str(bad)):
                compiler.compile_workspace(self.workspace(tmp, malformed), "receptionist")

    def test_editing_the_limit_after_signing_blocks_compilation_until_resigned(self):
        approved = signed({"id": "faq-book", "effect": "allow", "actions": ["calendar.book"], "conditions": {"maxPerPrincipalPerDay": 2}})
        edited = {**approved, "conditions": {"maxPerPrincipalPerDay": 2000}}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "faq-book.*sign-off"):
            compiler.compile_workspace(self.workspace(tmp, edited), "receptionist")

    def test_the_approvals_preview_shows_the_rule_as_conditional(self):
        booking = signed({"id": "limited-book", "effect": "allow", "actions": ["calendar.book"], "conditions": {"maxPerPrincipalPerDay": 1}})
        with tempfile.TemporaryDirectory() as tmp:
            report = compiler.workspace_approvals(self.workspace(tmp, booking), "receptionist", ())
        rows = {a["tool"]: a for entry in report[0]["skills"] for a in entry["actions"]}
        self.assertEqual(rows["calendar.book"]["member"], "conditional")


if __name__ == "__main__":
    unittest.main()
