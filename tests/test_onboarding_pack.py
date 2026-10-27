"""Credential-free checks for the owner-readable onboarding pack (#111, #114)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pack = load("onboarding_pack_under_test", "platform/core/onboarding_pack.py")
compiler = load("compiler_for_pack", "scripts/compile-workspace.py")
operator = load("operator_for_pack", "scripts/client-operator.py")
registry = load("registry_for_pack", "scripts/integration-directory.py").integrations

ZERO_STACK = "environment: dev\nruntime:\n  profile: receptionist\n  model_credential_ref: secret://model/dev\nconnectors:\n  kb:\n    provider: text-knowledge\n    tier: 0\n  crm:\n    provider: built-in-crm\n    tier: 0\n"
NO_CRM = "environment: dev\nruntime:\n  profile: receptionist\n  model_credential_ref: secret://model/dev\nconnectors:\n  kb:\n    provider: text-knowledge\n    tier: 0\n"


class Case(unittest.TestCase):
    def workspace(self, binding: str = ZERO_STACK, disabled: list[str] | None = None) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = pathlib.Path(tmp.name) / "client"
        shutil.copytree(ROOT / "examples/workspace", workspace)
        (workspace / "environments").mkdir()
        (workspace / "environments/dev.yaml").write_text(binding, encoding="utf-8")
        path = workspace / "agents/front-office/receptionist/agent.yaml"
        agent = yaml.safe_load(path.read_text())
        agent["spec"]["disabledSkills"] = [] if disabled is None else disabled
        path.write_text(yaml.safe_dump(agent, sort_keys=False))
        return workspace

    def packs(self, workspace: pathlib.Path, agent: str = "receptionist"):
        binding = compiler.load_environment_binding(workspace / "environments/dev.yaml")
        return pack.build(
            compiler.workspace_readiness(workspace, agent, (), binding),
            compiler.workspace_approvals(workspace, agent, ()),
            options=registry.options,
        )[0]


class BuildTests(Case):
    def test_a_zero_stack_receptionist_works_and_keeps_calendar_switched_off(self):
        result = self.packs(self.workspace(disabled=["book-appointment"]))
        self.assertEqual({skill["name"] for skill in result["working"]}, {"answer-faq", "take-message", "human-handoff", "log-conversation"})
        self.assertEqual(result["needsAction"], [])
        self.assertEqual(result["switchedOff"], ["book-appointment"])

    def test_a_blocked_skill_names_what_is_missing_and_the_one_next_step(self):
        result = self.packs(self.workspace(disabled=[]))
        blocked = {item["name"]: item for item in result["needsAction"]}
        self.assertEqual(set(blocked), {"book-appointment"})
        item = blocked["book-appointment"]
        self.assertIn("calendar: not connected yet", item["missing"])
        self.assertIn("connect calendar", item["action"])
        family = item["connect"][0]
        self.assertEqual((family["family"], family["command"]), ("calendar", "python3 scripts/client-operator.py connect calendar"))
        self.assertTrue(family["options"])

    def test_planned_providers_are_never_presented_as_usable(self):
        family = self.packs(self.workspace(disabled=[]))["needsAction"][0]["connect"][0]
        for option in family["options"]:
            self.assertEqual(option["usable"], option["status"] in pack.BUILT, option)
        self.assertFalse(any(option["usable"] for option in family["options"]), "no calendar connector is built credential-free")
        text = pack.render([self.packs(self.workspace(disabled=[]))])
        self.assertIn("No usable connector for this is shipped yet", text)

    def test_a_missing_tier_zero_dependency_blocks_only_its_skill(self):
        result = self.packs(self.workspace(binding=NO_CRM, disabled=["book-appointment"]))
        self.assertEqual({item["name"] for item in result["needsAction"]}, {"log-conversation"})
        self.assertIn("crm: not connected yet", result["needsAction"][0]["missing"])

    def test_actions_say_who_can_do_what_without_asking(self):
        result = self.packs(self.workspace(disabled=["book-appointment"]))
        by_tool = {action["tool"]: action for action in result["actions"]}
        self.assertEqual((by_tool["kb.search"]["team"], by_tool["kb.search"]["public"]), ("automatic", "automatic"))
        self.assertEqual((by_tool["crm.create"]["team"], by_tool["crm.create"]["public"]), ("approval", "denied"))
        self.assertEqual(by_tool["calendar.book"]["skill"], None)  # no active Skill uses it while booking is switched off
        self.assertTrue(result["policyVersion"].startswith("policy:"))

    def test_unknown_status_is_an_error_not_a_guess(self):
        report = [{"agent": "a", "environment": "dev", "skills": [
            {"name": "s", "kind": "Skill", "ready": False, "enabled": True,
             "dependencies": [{"capability": "x", "family": "x", "required": True, "status": "mystery"}]}]}]
        with self.assertRaises(pack.PackError):
            pack.build(report, [])

    def test_every_blocking_status_has_plain_words(self):
        for status in ("missing", "no-content", "unavailable", "no-grant", "grant-unavailable", "grant-scope"):
            report = [{"agent": "a", "environment": "dev", "skills": [
                {"name": "s", "kind": "Skill", "ready": False, "enabled": True, "remediation": "Do the thing",
                 "dependencies": [{"capability": "cap", "family": "calendar", "required": True, "status": status, "provider": "p", "tier": 1}]}]}]
            result = pack.build(report, [{"agent": "a", "policyVersion": "policy:test", "skills": []}])[0]
            self.assertTrue(result["needsAction"][0]["missing"][0].startswith("cap:"), status)

    def test_an_optional_missing_dependency_degrades_but_does_not_block(self):
        result = self.packs(self.workspace(disabled=["book-appointment"]))
        message = next(skill for skill in result["working"] if skill["name"] == "take-message")
        self.assertIn("tickets (optional): not connected; a simpler fallback (tier 4) is used instead", message["uses"])




class ReportIntegrityTests(unittest.TestCase):
    """Owner packs must not silently drop an Agent, a Skill or its approval policy."""

    @staticmethod
    def readiness(agent="front", skills=None):
        return {"agent": agent, "environment": "dev", "skills": skills if skills is not None else [
            {"name": "faq", "kind": "Skill", "enabled": True, "ready": True, "dependencies": [
                {"capability": "kb", "family": "kb", "required": True, "status": "bound",
                 "provider": "text-knowledge", "tier": 0}]}]}

    @staticmethod
    def approvals(agent="front"):
        return {"agent": agent, "policyVersion": "policy:test", "skills": [
            {"skill": "faq", "actions": [
                {"tool": "kb.search", "risk": "read", "member": "automatic", "public": "automatic"}]}]}

    def test_missing_approval_report_cannot_claim_no_actions_selected(self):
        with self.assertRaisesRegex(pack.PackError, "approval report"):
            pack.build([self.readiness()], [])
        with self.assertRaisesRegex(pack.PackError, "approval report"):
            pack.build([self.readiness(), self.readiness("other")], [self.approvals()])

    def test_extra_or_duplicate_agent_reports_are_rejected(self):
        with self.assertRaisesRegex(pack.PackError, "approval report"):
            pack.build([self.readiness()], [self.approvals(), self.approvals("other")])
        with self.assertRaisesRegex(pack.PackError, "duplicate approval"):
            pack.build([self.readiness()], [self.approvals(), self.approvals()])
        with self.assertRaisesRegex(pack.PackError, "duplicate readiness"):
            pack.build([self.readiness(), self.readiness()], [self.approvals()])

    def test_duplicate_skill_cannot_be_listed_twice_as_working(self):
        skill = self.readiness()["skills"][0]
        with self.assertRaisesRegex(pack.PackError, "duplicate Skill"):
            pack.build([self.readiness(skills=[skill, dict(skill)])], [self.approvals()])

    def test_required_unavailable_dependency_cannot_be_described_as_working(self):
        for status in ("missing", "no-content", "unavailable", "no-grant", "grant-unavailable", "grant-scope"):
            report = self.readiness()
            report["skills"][0]["dependencies"][0]["status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(pack.PackError, "claims ready"):
                pack.build([report], [self.approvals()])

    def test_missing_policy_version_is_not_presented_as_an_approval_preview(self):
        with self.assertRaisesRegex(pack.PackError, "policyVersion"):
            pack.build([self.readiness()], [{"agent": "front", "skills": []}])


class RenderTests(Case):
    def test_markdown_groups_by_what_the_owner_can_do(self):
        text = pack.render([self.packs(self.workspace(disabled=[]))])
        for heading in ("## What it can do now", "## What needs one thing from you first", "## What it will and will not do without asking you"):
            self.assertIn(heading, text)
        self.assertIn("- **book-appointment** cannot work yet: calendar: not connected yet.", text)
        self.assertIn("Connect it with: `python3 scripts/client-operator.py connect calendar`", text)
        self.assertIn("| crm.create | log-conversation | write | waits for a person's approval | is never done |", text)
        self.assertIn("A public caller can never approve", text)
        self.assertIn("does not show that any outside account is connected", text)

    def test_the_pack_never_carries_a_secret_reference_or_policy_internals(self):
        text = pack.render([self.packs(self.workspace(disabled=[]))])
        for forbidden in ("secret://", "model_credential_ref", "token_ref", "signoff", "ruleDigest", "conditions"):
            self.assertNotIn(forbidden, text)

    def test_the_renderer_refuses_anything_that_looks_like_a_credential(self):
        leaky = [{"agent": "a", "environment": "dev", "policyVersion": None, "working": [{"name": "s", "kind": "Skill", "uses": ["see secret://x/y"]}],
                  "needsAction": [], "switchedOff": [], "actions": []}]
        with self.assertRaises(pack.PackError):
            pack.render(leaky)

    def test_a_switched_off_skill_is_not_called_working(self):
        text = pack.render([self.packs(self.workspace(disabled=["book-appointment"]))])
        self.assertIn("## Switched off on purpose", text)
        self.assertIn("- book-appointment: listed for review but not active.", text)
        self.assertNotIn("**book-appointment**", text)


class CommandTests(Case):
    def run_command(self, *argv: str):
        out = io.StringIO()
        original = sys.argv
        sys.argv = ["client-operator.py", *argv]
        try:
            with contextlib.redirect_stdout(out):
                code = operator.main()
        finally:
            sys.argv = original
        return code, out.getvalue()

    def test_command_prints_the_pack(self):
        workspace = self.workspace(disabled=["book-appointment"])
        code, output = self.run_command("onboarding-pack", str(workspace), "--agent", "receptionist")
        self.assertEqual(code, 0)
        self.assertIn("# Your AI coworker: receptionist (dev)", output)
        self.assertNotIn("secret://", output)

    def test_command_writes_a_file(self):
        workspace = self.workspace(disabled=[])
        target = workspace.parent / "pack.md"
        code, output = self.run_command("onboarding-pack", str(workspace), "--agent", "receptionist", "--output", str(target))
        self.assertEqual(code, 0)
        self.assertIn("written", output)
        self.assertIn("book-appointment", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
