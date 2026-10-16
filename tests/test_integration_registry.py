from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "platform/core/integrations.py"
spec = importlib.util.spec_from_file_location("integrations_under_test", SOURCE)
registry = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(registry)


class IntegrationRegistryTests(unittest.TestCase):
    def test_registry_is_schema_valid_provider_neutral_and_accurate(self):
        providers = registry.load_registry()["providers"]
        self.assertGreaterEqual(len(providers), 18)
        self.assertEqual({p["id"] for p in providers if p["status"] == "built-credential-free"},
                         {"google-calendar", "microsoft-calendar", "text-knowledge", "built-in-crm"})
        self.assertFalse(any(p["status"] == "live" for p in providers))
        self.assertEqual({p["family"] for p in providers},
                         {"calendar", "crm", "mail", "kb", "docs", "files", "tickets", "chat", "code"})
        self.assertEqual(registry.options("calendar")[0]["id"], "google-calendar")

    def test_registry_cannot_claim_live_without_adapter_and_retained_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "integrations.json"
            original = registry.load_registry()
            for mutation, message in (
                ({"adapter": "platform/connectors/missing.py"}, "existing adapter"),
                ({"adapter": "platform/connectors/google_calendar.py", "status": "live"}, "retained proof"),
            ):
                altered = json.loads(json.dumps(original))
                altered["providers"][0].update(mutation)
                path.write_text(json.dumps(altered))
                with self.assertRaisesRegex(ValueError, message):
                    registry.load_registry(path)

    def test_unknown_tools_and_scope_overreach_cannot_enter_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "integrations.json"
            initial = registry.load_registry()
            for key, value, error in (
                ("tools", ["calendar.delete-all"], "unknown portable Tool"),
                ("scopeByTool", {"crm.update": ["unreviewed.scope"]}, "unlisted Tool"),
            ):
                altered = json.loads(json.dumps(initial))
                altered["providers"][0][key] = value
                path.write_text(json.dumps(altered))
                with self.assertRaisesRegex(ValueError, error):
                    registry.load_registry(path)

    def test_connection_preview_is_nonsecret_tool_scoped_and_nonmutating(self):
        read = registry.plan("calendar", "microsoft-calendar", ["calendar.availability"])
        self.assertEqual(read["scopes"], ["Calendars.Read"])
        self.assertFalse(read["readyToConnect"])
        self.assertIn("consent", read["blocker"])
        self.assertNotIn("Calendars.ReadWrite", json.dumps(read))
        both = registry.plan("calendar", "microsoft-calendar", ["calendar.book", "calendar.availability"])
        self.assertEqual(both["scopes"], ["Calendars.Read", "Calendars.ReadWrite"])
        with self.assertRaisesRegex(ValueError, "only the portable Tools"):
            registry.plan("calendar", "google-calendar", ["mail.send"])
        planned = registry.plan("crm", "zoho-crm", ["crm.read"])
        self.assertEqual(planned["status"], "planned")
        self.assertIn("not implemented", planned["blocker"])
        self.assertNotIn("scopes", planned)
        in_development = registry.plan("crm", "hubspot", ["crm.read"])
        self.assertEqual(in_development["status"], "in-development")
        self.assertIn("not implemented for the governed runtime", in_development["blocker"])
        self.assertIn("do not request credentials", in_development["blocker"])
        self.assertNotIn("scopes", in_development)  # no consent or scopes are offered for an unserved connector

    def test_tier_zero_text_provider_needs_no_credentials_and_says_what_the_owner_must_do(self):
        plan = registry.plan("kb", "text-knowledge", ["kb.search"])
        self.assertEqual((plan["status"], plan["tier"], plan["method"]), ("built-credential-free", 0, "file"))
        self.assertEqual(plan["scopes"], [])
        self.assertFalse(plan["readyToConnect"])
        self.assertIn("No credentials are needed", plan["blocker"])
        self.assertIn("approved", plan["blocker"])
        self.assertNotIn("consent", plan["blocker"])
        entry = next(p for p in registry.load_registry()["providers"] if p["id"] == "text-knowledge")
        self.assertEqual(entry["adapter"], "platform/core/knowledge.py")
        self.assertNotIn("proof", entry)
        self.assertNotEqual(entry["status"], "live")
        # The Tier 0 contact log is also credential-free; the third-party CRMs are still only planned.
        crm = registry.plan("crm", "built-in-crm", ["crm.read", "crm.create", "crm.log-interaction"])
        self.assertEqual((crm["status"], crm["tier"], crm["scopes"]), ("built-credential-free", 0, []))
        self.assertIn("No credentials are needed", crm["blocker"])
        self.assertIn("contacts review", crm["blocker"])
        self.assertNotIn("text file", crm["blocker"])
        with self.assertRaisesRegex(ValueError, "only the portable Tools"):
            registry.plan("crm", "built-in-crm", ["crm.update"])
        hubspot = registry.plan("crm", "hubspot", ["crm.read"])
        self.assertEqual(hubspot["status"], "in-development")
        self.assertIn("not implemented", hubspot["blocker"])
        self.assertEqual(registry.plan("crm", "zoho-crm", ["crm.read"])["status"], "planned")

    def test_directory_renderer_uses_registry_and_does_not_claim_live(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/integration-directory.py"), "--format", "markdown"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Microsoft Graph Calendar |", result.stdout)
        self.assertIn("built-credential-free", result.stdout)
        self.assertNotIn("| live |", result.stdout)
        self.assertIn("https://github.com/kingyx3/model-garden/issues/66", result.stdout)

    def test_operator_lists_providers_and_refuses_unimplemented_connect(self):
        cli = ROOT / "scripts/client-operator.py"
        listed = subprocess.run([sys.executable, str(cli), "connect", "crm"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("hubspot", listed.stdout)
        self.assertIn("built-in-crm", listed.stdout)
        planned = subprocess.run(
            [sys.executable, str(cli), "connect", "crm", "--provider", "hubspot",
             "--tools", "crm.read", "--owner", "owner@example.test", "--operator", "operator@example.test"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(planned.returncode, 1)
        self.assertIn("Connector not implemented", planned.stdout)
        ready = subprocess.run(
            [sys.executable, str(cli), "connect", "calendar", "--provider", "google-calendar",
             "--tools", "calendar.availability", "--owner", "owner@example.test", "--operator", "operator@example.test"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        result = json.loads(ready.stdout)
        self.assertEqual(result["scopes"], ["https://www.googleapis.com/auth/calendar.freebusy"])
        self.assertFalse(result["readyToConnect"])
        self.assertNotIn("secret://", ready.stdout)

    def test_blocked_skill_report_suggests_canonical_connect_action(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("compile_registry_readiness", ROOT / "scripts/compile-workspace.py")
        compiler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiler)
        result = compiler.workspace_readiness(
            ROOT / "examples/workspace", "receptionist", (), {"environment": "dev"})
        report = compiler.format_readiness(result)
        self.assertIn("connect calendar", report)
        self.assertIn("google-calendar", report)
        self.assertNotIn("secret://", report)


if __name__ == "__main__":
    unittest.main()
