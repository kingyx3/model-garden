from __future__ import annotations

import copy
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


policy = _load("policy_under_test", "platform/core/policy.py")
govern = _load("govern_action_for_policy", "scripts/govern-action.py")
compiler = _load("compile_workspace_for_policy", "scripts/compile-workspace.py")


def signed(rule: dict, owner: str = "owner@client.example") -> dict:
    return {**rule, "signoff": {"owner": owner, "ruleDigest": policy.rule_digest(rule)}}


def policy_doc(*rules: dict) -> dict:
    return {
        "apiVersion": "modelgarden.ai/v1",
        "kind": "Policy",
        "metadata": {"name": "client-policy", "owner": "client"},
        "spec": {"rules": list(rules)},
    }


def evaluate(*rules: dict, tool: str = "calendar.book", risk: str = "write", **kwargs) -> dict:
    return policy.evaluate([policy_doc(*rules)] if rules else None, tool_name=tool, risk=risk, **kwargs)


class PolicyEvaluationTests(unittest.TestCase):
    def test_no_policy_keeps_agent_and_tool_behaviour(self):
        self.assertEqual(evaluate()["reason"], "selected-tool")
        self.assertEqual(evaluate(tool_requires_approval=True)["reason"], "tool-requires-approval")
        self.assertEqual(evaluate(agent_requires_approval=True, tool_requires_approval=True)["reason"], "agent-requires-approval")
        self.assertEqual(policy.policy_version(None), policy.DEFAULT_VERSION)

    def test_deny_rule_wins_over_a_signed_allow(self):
        allow = signed({"id": "allow-book", "effect": "allow", "actions": ["calendar.book"]})
        deny = {"id": "no-book", "effect": "deny", "actions": ["calendar.*"]}
        result = evaluate(allow, deny)
        self.assertEqual((result["decision"], result["reason"], result["ruleId"]), ("deny", "policy-deny", "no-book"))

    def test_stricter_source_wins_over_a_signed_allow(self):
        allow = signed({"id": "allow-book", "effect": "allow", "actions": ["calendar.book"]})
        approve = {"id": "approve-book", "effect": "require-approval", "actions": ["calendar.book"]}
        self.assertEqual(evaluate(allow, approve, tool_requires_approval=True)["ruleId"], "approve-book")
        agent = evaluate(allow, agent_requires_approval=True, tool_requires_approval=True)
        self.assertEqual((agent["decision"], agent["reason"]), ("require-approval", "agent-requires-approval"))

    def test_only_a_valid_signed_allow_waives_the_tool_default(self):
        rule = {"id": "allow-book", "effect": "allow", "actions": ["calendar.book"]}
        self.assertEqual(evaluate(rule, tool_requires_approval=True)["decision"], "require-approval")
        wrong = {**rule, "signoff": {"owner": "owner", "ruleDigest": "0" * 64}}
        self.assertEqual(evaluate(wrong, tool_requires_approval=True)["decision"], "require-approval")
        blank_owner = {**rule, "signoff": {"owner": " ", "ruleDigest": policy.rule_digest(rule)}}
        self.assertEqual(evaluate(blank_owner, tool_requires_approval=True)["decision"], "require-approval")
        waived = evaluate(signed(rule), tool_requires_approval=True)
        self.assertEqual((waived["decision"], waived["ruleId"]), ("allow", "allow-book"))

    def test_editing_a_signed_rule_invalidates_its_signoff(self):
        rule = signed({"id": "allow-book", "effect": "allow", "actions": ["calendar.book"]})
        edited = {**rule, "actions": ["calendar.book", "mail.send"]}
        self.assertFalse(policy.signoff_valid(edited))
        self.assertEqual(evaluate(edited, tool_requires_approval=True)["decision"], "require-approval")

    def test_privileged_tools_are_never_auto_approved_or_public(self):
        rule = signed({"id": "allow-share", "effect": "allow", "actions": ["files.share"], "principals": ["member", "public"]})
        member = evaluate(rule, tool="files.share", risk="privileged")
        self.assertEqual((member["decision"], member["reason"]), ("require-approval", "privileged-requires-approval"))
        public = evaluate(rule, tool="files.share", risk="privileged", principal="public")
        self.assertEqual((public["decision"], public["reason"]), ("deny", "principal-not-authorized"))

    def test_public_principals_are_limited_to_reads_unless_a_signed_rule_names_them(self):
        self.assertEqual(evaluate(tool="calendar.availability", risk="read", principal="public")["decision"], "allow")
        self.assertEqual(evaluate(principal="public")["reason"], "principal-not-authorized")
        member_only = signed({"id": "allow-book", "effect": "allow", "actions": ["calendar.book"]})
        self.assertEqual(evaluate(member_only, principal="public")["reason"], "principal-not-authorized")
        unsigned_public = {"id": "public-book", "effect": "allow", "actions": ["calendar.book"], "principals": ["public"]}
        self.assertEqual(evaluate(unsigned_public, principal="public")["reason"], "principal-not-authorized")
        public_rule = signed({"id": "public-book", "effect": "allow", "actions": ["calendar.book"], "principals": ["public"]})
        granted = evaluate(public_rule, principal="public", tool_requires_approval=True)
        self.assertEqual((granted["decision"], granted["ruleId"]), ("allow", "public-book"))
        # The Agent-wide switch still holds for a public caller, who cannot approve it.
        held = evaluate(public_rule, principal="public", agent_requires_approval=True)
        self.assertEqual(held["decision"], "require-approval")

    def test_public_principals_never_reach_external_communication(self):
        rule = signed({"id": "public-send", "effect": "allow", "actions": ["mail.send"], "principals": ["public"]})
        result = evaluate(rule, tool="mail.send", risk="external-communication", principal="public")
        self.assertEqual(result["reason"], "principal-not-authorized")

    def test_restrictive_rules_cover_everyone_and_loosening_covers_members(self):
        approve = {"id": "approve-mail", "effect": "require-approval", "actions": ["mail.send"]}
        for who in ("member", "public"):
            result = evaluate(approve, tool="mail.send", risk="read", principal=who)
            self.assertEqual(result["ruleId"], "approve-mail")

    def test_channel_and_classification_narrow_rules_and_fail_closed_when_unknown(self):
        approve_voice = {"id": "voice-approve", "effect": "require-approval", "actions": ["calendar.book"], "channels": ["voice"]}
        self.assertEqual(evaluate(approve_voice, channel="voice")["decision"], "require-approval")
        self.assertEqual(evaluate(approve_voice, channel="slack")["decision"], "allow")
        self.assertEqual(evaluate(approve_voice)["decision"], "require-approval")  # unknown channel: fail closed
        allow_voice = signed({"id": "voice-allow", "effect": "allow", "actions": ["calendar.book"], "channels": ["voice"]})
        self.assertEqual(evaluate(allow_voice, tool_requires_approval=True, channel="voice")["decision"], "allow")
        self.assertEqual(evaluate(allow_voice, tool_requires_approval=True)["decision"], "require-approval")
        restricted = {"id": "restricted", "effect": "deny", "dataClassifications": ["restricted"]}
        self.assertEqual(evaluate(restricted, classification="restricted")["decision"], "deny")
        self.assertEqual(evaluate(restricted, classification="internal")["decision"], "allow")
        self.assertEqual(evaluate(restricted)["decision"], "deny")  # unknown classification: fail closed

    def test_read_allow_rule_is_recorded_without_changing_approval(self):
        rule = {"id": "allow-reads", "effect": "allow", "actions": ["*.read"]}
        result = evaluate(rule, tool="crm.read", risk="read")
        self.assertEqual((result["decision"], result["reason"], result["ruleId"]), ("allow", "policy-allow", "allow-reads"))
        self.assertEqual(evaluate(rule, tool="crm.read", risk="read", agent_requires_approval=True)["decision"], "require-approval")


class PolicyChecksTests(unittest.TestCase):
    TOOLS = [
        {"metadata": {"name": "calendar.availability"}, "spec": {"risk": "read"}},
        {"metadata": {"name": "calendar.book"}, "spec": {"risk": "write", "approvalRequired": True}},
        {"metadata": {"name": "mail.send"}, "spec": {"risk": "external-communication"}},
    ]

    def errors(self, *rules: dict) -> list[str]:
        return policy.relaxation_errors([policy_doc(*rules)], self.TOOLS)

    def test_read_only_allow_rule_needs_no_signoff(self):
        self.assertEqual(self.errors({"id": "reads", "effect": "allow", "actions": ["*.availability"]}), [])

    def test_relaxing_rule_requires_owner_signoff_bound_to_content(self):
        rule = {"id": "book", "effect": "allow", "actions": ["calendar.book"]}
        problems = self.errors(rule)
        self.assertEqual(len(problems), 1)
        self.assertIn("client owner's sign-off", problems[0])
        self.assertIn(policy.rule_digest(rule), problems[0])
        self.assertEqual(self.errors(signed(rule)), [])
        tampered = {**signed(rule), "actions": ["calendar.book", "mail.send"]}
        self.assertEqual(len(self.errors(tampered)), 1)

    def test_structural_errors_are_reported(self):
        problems = self.errors(
            {"id": "dup", "effect": "deny", "actions": ["mail.send"]},
            {"id": "dup", "effect": "require-approval", "actions": ["calendar.book"]},
            {"id": "blank", "effect": "allow"},
        )
        self.assertTrue(any("more than once" in item for item in problems))
        self.assertTrue(any("must list the actions" in item for item in problems))

    def test_public_rule_cannot_name_privileged_or_external_communication_tools(self):
        rule = signed({"id": "public-send", "effect": "allow", "actions": ["mail.send"], "principals": ["public"]})
        problems = self.errors(rule)
        self.assertTrue(any("public principal" in item and "mail.send" in item for item in problems))

    def test_policy_diff_flags_relaxation(self):
        base = [policy_doc({"id": "approve", "effect": "require-approval", "actions": ["mail.send"]})]
        loosened = [policy_doc(
            {"id": "approve", "effect": "allow", "actions": ["mail.send"]},
            {"id": "extra", "effect": "deny", "actions": ["files.share"]},
        )]
        diff = policy.diff_policies(base, loosened)
        self.assertEqual((diff["added"], diff["removed"], diff["modified"]), (["extra"], [], ["approve"]))
        self.assertEqual(diff["relaxed"], ["approve"])
        self.assertNotEqual(diff["previousVersion"], diff["newVersion"])
        self.assertEqual(policy.diff_policies(base, copy.deepcopy(base))["relaxed"], [])
        removed = policy.diff_policies(base, [])
        self.assertEqual((removed["removed"], removed["relaxed"]), (["approve"], ["approve"]))


def desired_state(*rules: dict, agent_requires: bool = False) -> dict:
    return {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "agent": {"spec": {
            "approvals": {"externalActions": "required" if agent_requires else "none"},
            "security": {"dataClassification": "confidential"},
        }},
        "tools": [
            {"metadata": {"name": "calendar.book"}, "spec": {"type": "internal", "risk": "write", "approvalRequired": True}},
            {"metadata": {"name": "calendar.availability"}, "spec": {"type": "internal", "risk": "read"}},
        ],
        **({"policies": [policy_doc(*rules)]} if rules else {}),
    }


class GovernedBoundaryPolicyTests(unittest.TestCase):
    ARGS = {"start": "2026-10-20T10:00:00+08:00", "end": "2026-10-20T10:30:00+08:00"}

    def read_events(self, path: pathlib.Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_ledger_records_matching_rule_and_policy_version(self):
        rule = signed({"id": "voice-booking", "effect": "allow", "actions": ["calendar.book"]})
        state = desired_state(rule)
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            result = govern.execute_action(state, "calendar.book", self.ARGS, lambda args: {"eventId": "evt-1"}, audit_path=audit)
            self.assertEqual(result["status"], "executed")
            ledgers = [event["ledger"] for event in self.read_events(audit)]
            self.assertTrue(all(item["policyRuleId"] == "voice-booking" for item in ledgers))
            self.assertTrue(all(item["policyVersion"] == policy.policy_version(state["policies"]) for item in ledgers))
            self.assertNotEqual(ledgers[0]["policyVersion"], govern.POLICY_VERSION)
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])

    def test_without_policy_the_legacy_ledger_fields_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            result = govern.execute_action(desired_state(), "calendar.book", self.ARGS, lambda args: args, audit_path=audit)
            self.assertEqual(result["status"], "pending-approval")
            ledger = self.read_events(audit)[0]["ledger"]
            self.assertEqual((ledger["policyRuleId"], ledger["policyVersion"]), ("tool-requires-approval", govern.POLICY_VERSION))

    def test_policy_deny_blocks_execution_and_names_the_rule(self):
        state = desired_state({"id": "no-booking", "effect": "deny", "actions": ["calendar.book"]})
        calls: list[dict] = []
        with self.assertRaisesRegex(PermissionError, "no-booking"):
            govern.execute_action(state, "calendar.book", self.ARGS, calls.append)
        self.assertEqual(calls, [])

    def test_signed_public_rule_lets_a_public_caller_book_and_nothing_else(self):
        rule = signed({"id": "public-booking", "effect": "allow", "actions": ["calendar.book"], "principals": ["public"]})
        state = desired_state(rule)
        calls: list[dict] = []
        result = govern.execute_action(state, "calendar.book", self.ARGS, lambda args: calls.append(args) or {"eventId": "e"}, principal_access="public")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(len(calls), 1)
        plain = desired_state()
        with self.assertRaises(PermissionError):
            govern.execute_action(plain, "calendar.book", self.ARGS, calls.append, principal_access="public")

    def test_channel_from_governance_context_selects_the_rule(self):
        rule = {"id": "voice-approval", "effect": "require-approval", "actions": ["calendar.availability"], "channels": ["voice"]}
        state = desired_state(rule)
        on_voice = govern.authorize_action(state, "calendar.availability", self.ARGS, governance_context={"channel": "voice"})
        on_slack = govern.authorize_action(state, "calendar.availability", self.ARGS, governance_context={"channel": "slack"})
        self.assertEqual((on_voice["decision"], on_voice["ruleId"]), ("require-approval", "voice-approval"))
        self.assertEqual(on_slack["decision"], "allow")

    def test_policy_change_is_ledgered_with_author_and_diff(self):
        before = [policy_doc({"id": "approve", "effect": "require-approval", "actions": ["calendar.book"]})]
        after = [policy_doc({"id": "approve", "effect": "allow", "actions": ["calendar.book"]})]
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self.assertIsNone(govern.record_policy_change(audit, before, copy.deepcopy(before), changed_by="consultant@mg.example"))
            event = govern.record_policy_change(audit, before, after, changed_by="consultant@mg.example")
            self.assertEqual((event["event"], event["changedBy"]), ("policy-change", "consultant@mg.example"))
            self.assertEqual(event["details"]["relaxed"], ["approve"])
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])
            with self.assertRaises(ValueError):
                govern.record_policy_change(audit, before, after, changed_by=" ")


class CompiledPolicyTests(unittest.TestCase):
    def workspace(self, tmp: str, *extra_rules: dict) -> pathlib.Path:
        target = pathlib.Path(tmp) / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", target)
        agent_file = target / "agents/front-office/receptionist/agent.yaml"
        agent = yaml.safe_load(agent_file.read_text())
        agent["spec"]["disabledSkills"] = []
        agent_file.write_text(yaml.safe_dump(agent))
        policy_path = target / "policies" / "default.yaml"
        document = yaml.safe_load(policy_path.read_text())
        document["spec"]["rules"].extend(extra_rules)
        policy_path.write_text(yaml.safe_dump(document))
        return target

    def test_example_workspace_compiles_policies_into_desired_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            compiled = compiler.compile_workspace(self.workspace(tmp), "receptionist")[0]
        self.assertEqual([item["metadata"]["name"] for item in compiled["policies"]], ["default-agent-policy"])

    def test_unsigned_relaxing_rule_fails_compilation_until_the_owner_signs(self):
        rule = {"id": "auto-book", "effect": "allow", "actions": ["calendar.book"]}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "auto-book.*sign-off"):
                compiler.compile_workspace(self.workspace(tmp, rule), "receptionist")
        with tempfile.TemporaryDirectory() as tmp:
            compiled = compiler.compile_workspace(self.workspace(tmp, signed(rule)), "receptionist")[0]
            outcome = govern.authorize_action(compiled, "calendar.book", {"start": "x", "end": "y"})
        self.assertEqual((outcome["decision"], outcome["ruleId"]), ("allow", "auto-book"))

    def test_policy_reference_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(tmp)
            agent = workspace / "agents" / "front-office" / "receptionist" / "agent.yaml"
            agent.write_text(agent.read_text().replace("default-agent-policy", "missing-policy"))
            with self.assertRaisesRegex(ValueError, "missing Policy 'missing-policy'"):
                compiler.compile_workspace(workspace, "receptionist")

    def test_approval_preview_lists_automatic_approval_and_denied_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = compiler.workspace_approvals(self.workspace(tmp), "receptionist", ())
            text = compiler.format_approvals(report)
        rows = {action["tool"]: action for entry in report[0]["skills"] for action in entry["actions"]}
        self.assertEqual((rows["calendar.availability"]["member"], rows["calendar.availability"]["public"]), ("automatic", "automatic"))
        self.assertEqual((rows["calendar.book"]["member"], rows["calendar.book"]["public"]), ("approval", "denied"))
        self.assertIn("calendar.book", text)
        self.assertIn("public callers never approve", text)


if __name__ == "__main__":
    unittest.main()
