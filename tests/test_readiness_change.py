"""Credential-free checks for the read-only preview of what a connection change does to Skill readiness (#110, #111)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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


change = load("readiness_change_under_test", "platform/core/readiness_change.py")
operator = load("operator_for_change", "scripts/client-operator.py")

TIER_ZERO = "connectors:\n  kb:\n    provider: text-knowledge\n    tier: 0\n  crm:\n    provider: built-in-crm\n    tier: 0\n"
HEAD = "environment: dev\nruntime:\n  profile: receptionist\n  model_credential_ref: secret://model/dev\n"
NO_CRM = HEAD + "connectors:\n  kb:\n    provider: text-knowledge\n    tier: 0\n"


def entry(name, *, ready=True, enabled=True, remediation=None, kind="Skill"):
    return {"name": name, "kind": kind, "ready": ready, "enabled": enabled, "dependencies": [], **({"remediation": remediation} if remediation else {})}


def report(*entries, agent="a"):
    return [{"agent": agent, "environment": "dev", "skills": list(entries)}]


def outcomes(result):
    return {row["name"]: row["outcome"] for row in result[0]["changes"]}


class DiffTests(unittest.TestCase):
    def test_each_transition_has_its_own_outcome(self):
        before = report(entry("a", ready=False), entry("b"), entry("c", ready=False), entry("d"), entry("e", enabled=False, ready=False),
                        entry("f"), entry("g", enabled=False, ready=True))
        after = report(entry("a"), entry("b", ready=False, remediation="Connect crm"), entry("c", ready=False, remediation="Connect mail"),
                       entry("d"), entry("e", enabled=False, ready=False), entry("h"), entry("g"))
        result = change.diff(before, after)
        self.assertEqual(outcomes(result), {"a": "unblocked", "b": "regressed", "c": "still-blocked", "d": "unchanged", "e": "switched-off",
                                            "f": "removed", "h": "added", "g": "unblocked"})
        self.assertTrue(result[0]["regressed"])
        rows = {row["name"]: row for row in result[0]["changes"]}
        self.assertEqual(rows["b"]["next"], "Connect crm")
        self.assertEqual(rows["c"]["next"], "Connect mail")
        self.assertNotIn("next", rows["a"])

    def test_removing_a_skill_that_was_ready_counts_as_a_regression_but_not_one_that_was_not(self):
        self.assertTrue(change.diff(report(entry("x")), report())[0]["regressed"])
        self.assertFalse(change.diff(report(entry("x", ready=False)), report())[0]["regressed"])

    def test_an_agent_missing_after_is_an_error(self):
        with self.assertRaises(ValueError):
            change.diff(report(entry("x")), report(entry("x"), agent="other"))

    def test_new_agent_is_not_silently_omitted_from_owner_preview(self):
        before = report(entry("existing"), agent="receptionist")
        after = before + report(entry("book", ready=False, remediation="Connect calendar"),
                                entry("faq"), agent="sales")
        result = change.diff(before, after)
        self.assertEqual([item["agent"] for item in result], ["receptionist", "sales"])
        self.assertTrue(result[1]["addedAgent"])
        self.assertEqual({row["name"]: row["outcome"] for row in result[1]["changes"]},
                         {"book": "added", "faq": "added"})
        text = change.render(result)
        self.assertIn("Agent sales (new)", text)
        self.assertIn("- book (blocked) — next step: Connect calendar", text)
        self.assertIn("- faq (ready)", text)

    def test_duplicate_agents_or_skills_fail_closed_instead_of_hiding_changes(self):
        duplicate_agents = report(entry("x")) + report(entry("y"))
        with self.assertRaisesRegex(ValueError, "duplicate Agent"):
            change.diff(duplicate_agents, report(entry("x")))
        with self.assertRaisesRegex(ValueError, "duplicate Agent"):
            change.diff(report(entry("x")), duplicate_agents)
        duplicate_skills = report(entry("x"), entry("x"))
        with self.assertRaisesRegex(ValueError, "duplicate Skill"):
            change.diff(duplicate_skills, report(entry("x")))
        with self.assertRaisesRegex(ValueError, "duplicate Skill"):
            change.diff(report(entry("x")), duplicate_skills)

    def test_added_blocked_skill_has_action_and_disabled_skill_is_not_ready(self):
        before = report()
        after = report(entry("blocked", ready=False, remediation="Connect crm"),
                       entry("off", ready=True, enabled=False))
        result = change.diff(before, after)
        rows = {row["name"]: row for row in result[0]["changes"]}
        self.assertEqual(rows["blocked"]["next"], "Connect crm")
        self.assertEqual(rows["blocked"]["state"], "blocked")
        self.assertEqual(rows["off"]["state"], "off")
        rendered = change.render(result)
        self.assertIn("- blocked (blocked) — next step: Connect crm", rendered)
        self.assertIn("- off (switched off)", rendered)

    def test_render_puts_the_warning_first_and_names_the_next_step(self):
        before = report(entry("keeps"), entry("breaks"), entry("fixes", ready=False))
        after = report(entry("keeps"), entry("breaks", ready=False, remediation="Reconnect crm"), entry("fixes"))
        text = change.render(change.diff(before, after), subject="binding crm to hubspot")
        self.assertIn("what binding crm to hubspot would do", text)
        self.assertLess(text.index("WARNING"), text.index("Starts working"))
        self.assertIn("- breaks — next step: Reconnect crm", text)
        self.assertIn("Starts working:\n    - fixes", text)
        self.assertIn("Already working, unchanged:\n    - keeps", text)

    def test_nothing_secret_can_appear(self):
        text = change.render(change.diff(report(entry("x")), report(entry("x"))))
        self.assertNotIn("secret://", text)


class CommandTests(unittest.TestCase):
    def workspace(self, binding: str, disabled=("book-appointment",)) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = pathlib.Path(tmp.name) / "client"
        shutil.copytree(ROOT / "examples/workspace", workspace)
        (workspace / "environments").mkdir()
        (workspace / "environments/dev.yaml").write_text(binding, encoding="utf-8")
        path = workspace / "agents/front-office/receptionist/agent.yaml"
        agent = yaml.safe_load(path.read_text())
        agent["spec"]["disabledSkills"] = list(disabled)
        path.write_text(yaml.safe_dump(agent, sort_keys=False))
        return workspace

    def run_command(self, workspace, *argv):
        out = io.StringIO()
        original = sys.argv
        sys.argv = ["client-operator.py", "readiness-change", str(workspace), *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = operator.main()
        finally:
            sys.argv = original
        return code, out.getvalue()

    def test_binding_the_missing_tier_zero_crm_unblocks_its_skill_and_writes_nothing(self):
        workspace = self.workspace(HEAD + "connectors:\n  kb:\n    provider: text-knowledge\n    tier: 0\n")
        before = (workspace / "environments/dev.yaml").read_text()
        code, output = self.run_command(workspace, "crm", "--provider", "built-in-crm", "--tier", "0", "--agent", "receptionist", "--json")
        self.assertEqual(code, 0)
        result = json.loads(output[: output.rindex("]") + 1])
        self.assertEqual(outcomes(result)["log-conversation"], "unblocked")
        self.assertEqual(outcomes(result)["book-appointment"], "switched-off")
        self.assertEqual((workspace / "environments/dev.yaml").read_text(), before)

    def test_text_output_says_nothing_was_written(self):
        workspace = self.workspace(NO_CRM)
        code, output = self.run_command(workspace, "crm", "--provider", "built-in-crm", "--tier", "0", "--agent", "receptionist")
        self.assertEqual(code, 0)
        self.assertIn("Starts working:\n    - log-conversation", output)
        self.assertIn("Nothing was written: this is a preview.", output)

    def test_an_unshipped_provider_never_makes_a_skill_work(self):
        workspace = self.workspace(NO_CRM)
        code, output = self.run_command(workspace, "crm", "--provider", "hubspot", "--tier", "2", "--agent", "receptionist")
        self.assertNotIn("Starts working", output)
        self.assertIn("Still needs a step", output)
        self.assertIn("log-conversation", output)

    def test_replacing_a_working_provider_must_be_explicit_and_shows_the_breakage(self):
        workspace = self.workspace(HEAD + TIER_ZERO)
        code, output = self.run_command(workspace, "crm", "--provider", "hubspot", "--tier", "2", "--agent", "receptionist")
        self.assertEqual(code, 1)  # refused: a provider replacement is explicit (ConnectError), nothing previewed
        self.assertNotIn("Starts working", output)
        code, output = self.run_command(workspace, "crm", "--provider", "hubspot", "--tier", "2", "--replace", "--agent", "receptionist")
        self.assertEqual(code, 1)
        self.assertIn("WARNING: this would stop something that works today", output)
        self.assertIn("log-conversation", output)

    def test_widening_scopes_needs_the_rescope_flag(self):
        workspace = self.workspace(HEAD + "connectors:\n  kb:\n    provider: text-knowledge\n    tier: 0\n  crm:\n    provider: built-in-crm\n    tier: 0\n    scopes: [read]\n")
        code, _ = self.run_command(workspace, "crm", "--provider", "built-in-crm", "--tier", "0", "--scopes", "read,write", "--agent", "receptionist")
        self.assertEqual(code, 1)
        code, _ = self.run_command(workspace, "crm", "--provider", "built-in-crm", "--tier", "0", "--scopes", "read,write", "--rescope", "--agent", "receptionist")
        self.assertEqual(code, 0)

    def test_a_calendar_for_an_enabled_booking_skill_stays_blocked_until_a_shipped_connector_exists(self):
        workspace = self.workspace(HEAD + TIER_ZERO, disabled=())
        code, output = self.run_command(workspace, "calendar", "--provider", "google-calendar", "--tier", "2", "--agent", "receptionist")
        self.assertIn("book-appointment", output.split("Still needs a step:")[1])
        self.assertNotIn("Starts working", output)


if __name__ == "__main__":
    unittest.main()
