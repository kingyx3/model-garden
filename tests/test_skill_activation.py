"""Credential-free regression tests for per-Agent Skill activation and runtime health."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


compiler = load("compiler_skill_activation", "scripts/compile-workspace.py")
capabilities = load("capabilities_skill_activation", "platform/core/capabilities.py")
provisioner = load("hermes_skill_activation", "scripts/provision-hermes-profile.py")

# The receptionist's knowledge and contact-log dependencies are satisfied at Tier 0 with no
# third-party account; a client with no calendar simply keeps booking disabled.
TIER_ZERO = {
    "kb": {"provider": "text-knowledge", "tier": 0},
    "crm": {"provider": "built-in-crm", "tier": 0},
}
ZERO_STACK = {"environment": "dev", "connectors": dict(TIER_ZERO)}
WITH_CALENDAR = {
    "environment": "dev",
    "connectors": {**TIER_ZERO, "calendar": {"provider": "google", "token_ref": "secret://google/dev"}},
}


class SkillActivationTests(unittest.TestCase):
    def workspace(self, root: pathlib.Path) -> pathlib.Path:
        workspace = root / "client"
        shutil.copytree(ROOT / "examples/workspace", workspace)
        (workspace / "environments").mkdir()
        (workspace / "environments/dev.yaml").write_text(
            "environment: dev\nruntime:\n  profile: receptionist\n  model_credential_ref: secret://model/dev\n"
            "connectors:\n  kb:\n    provider: text-knowledge\n    tier: 0\n  crm:\n    provider: built-in-crm\n    tier: 0\n",
            encoding="utf-8",
        )
        return workspace

    @staticmethod
    def edit_agent(workspace: pathlib.Path, *, disabled: list[str]) -> pathlib.Path:
        path = workspace / "agents/front-office/receptionist/agent.yaml"
        agent = yaml.safe_load(path.read_text())
        agent["spec"]["disabledSkills"] = disabled
        path.write_text(yaml.safe_dump(agent, sort_keys=False))
        return path

    def test_disabled_skill_not_compiled_materialized_or_granted_exclusive_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            self.edit_agent(workspace, disabled=["book-appointment"])
            desired = compiler.compile_workspace(
                workspace, "receptionist", environment_binding=ZERO_STACK
            )[0]
            names = {x["metadata"]["name"] for x in desired["skills"]}
            self.assertNotIn("book-appointment", names)
            # Only the calendar Tools exclusive to the disabled Skill are suppressed.
            self.assertEqual(
                [x["metadata"]["name"] for x in desired["tools"]],
                ["kb.search", "crm.create", "crm.log-interaction"],
            )
            self.assertNotIn("book-appointment", desired["skillInstructions"])
            self.assertEqual(
                desired["disabledSkillDeclarations"][0]["spec"]["requiredCapabilities"], ["calendar"]
            )
            self.assertEqual(
                set(desired["disabledSkillDeclarations"][0]),
                {"metadata", "spec"},
            )
            by_name = {x["name"]: x for x in desired["capabilities"]["skills"]}
            self.assertFalse(by_name["book-appointment"]["enabled"])
            self.assertFalse(by_name["book-appointment"]["ready"])
            self.assertFalse(by_name["book-appointment"]["canEnable"])
            manifest = capabilities.build_manifest(desired, {"environment": "dev"})
            manifest_skill = next(x for x in manifest["skills"] if x["name"] == "book-appointment")
            self.assertFalse(manifest_skill["enabled"])
            self.assertEqual(manifest_skill["dependencies"][0]["status"], "missing")
            files = provisioner.build_profile_files(desired)
            self.assertFalse(any("book-appointment" in p.as_posix() for p in files))
            self.assertNotIn("secret://", json.dumps(manifest))

    def test_disabled_skill_is_explained_even_when_compiled_without_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            self.edit_agent(workspace, disabled=["book-appointment"])
            desired = compiler.compile_workspace(workspace, "receptionist")[0]
            self.assertNotIn("book-appointment", [
                skill["metadata"]["name"] for skill in desired["skills"]
            ])
            manifest = capabilities.build_manifest(desired, {"environment": "dev"})
            booking = next(s for s in manifest["skills"] if s["name"] == "book-appointment")
            self.assertFalse(booking["enabled"])
            self.assertFalse(booking["canEnable"])
            self.assertEqual(booking["dependencies"][0]["status"], "missing")
            self.assertNotIn("calendar.book", [
                tool for entry in manifest["capabilities"] for tool in entry["tools"]
            ])

    def test_enabling_blocked_skill_fails_closed_then_succeeds_with_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            path = self.edit_agent(workspace, disabled=["book-appointment"])
            readiness = compiler.workspace_readiness(workspace, "receptionist", (), {"environment": "dev"})
            by_name = {skill["name"]: skill for skill in readiness[0]["skills"]}
            self.assertFalse(by_name["book-appointment"]["enabled"])
            self.assertIn("DISABLED", compiler.format_readiness(readiness))
            agent = yaml.safe_load(path.read_text())
            agent["spec"]["disabledSkills"] = []
            path.write_text(yaml.safe_dump(agent, sort_keys=False))
            with self.assertRaisesRegex(ValueError, "book-appointment"):
                compiler.compile_workspace(workspace, "receptionist",
                                           environment_binding={"environment": "dev"})
            ready = compiler.compile_workspace(workspace, "receptionist", environment_binding=WITH_CALENDAR)[0]
            self.assertIn("book-appointment", [s["metadata"]["name"] for s in ready["skills"]])
            self.assertIn("calendar.book", [t["metadata"]["name"] for t in ready["tools"]])

    def test_disabled_must_be_explicitly_listed_and_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            self.edit_agent(workspace, disabled=["not-selected"])
            with self.assertRaisesRegex(ValueError, "disables Skills it did not select"):
                compiler.compile_workspace(workspace, "receptionist", environment_binding={"environment": "dev"})
            with self.assertRaisesRegex(ValueError, "disables Skills it did not select"):
                compiler.workspace_readiness(workspace, "receptionist", (), {"environment": "dev"})

    def test_shared_tool_remains_only_if_required_by_active_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            self.edit_agent(workspace, disabled=["book-appointment"])
            path = workspace / "skills/human-handoff/skill.yaml"
            skill = yaml.safe_load(path.read_text())
            skill["spec"]["allowedTools"] = ["calendar.availability"]
            path.write_text(yaml.safe_dump(skill, sort_keys=False))
            desired = compiler.compile_workspace(
                workspace, "receptionist", environment_binding=ZERO_STACK
            )[0]
            self.assertEqual(
                [t["metadata"]["name"] for t in desired["tools"]],
                ["calendar.availability", "kb.search", "crm.create", "crm.log-interaction"],
            )

    def test_runtime_revocation_recomputes_required_skill_readiness(self):
        compiled = compiler.compile_workspace(
            ROOT / "examples/workspace", "receptionist", environment_binding=WITH_CALENDAR
        )[0]
        manifest = capabilities.build_manifest(compiled, WITH_CALENDAR)
        before = next(x for x in manifest["skills"] if x["name"] == "book-appointment")
        self.assertTrue(before["ready"])
        assert manifest["capabilities"]
        revoked = capabilities.apply_connection_health(
            manifest, {"calendar": {"health": "revoked", "updatedAt": "2026-10-14T00:00:00Z"}}
        )
        after = next(x for x in revoked["skills"] if x["name"] == "book-appointment")
        self.assertFalse(after["ready"])
        self.assertFalse(after["canEnable"])
        self.assertEqual(after["dependencies"][0]["status"], "unavailable")
        self.assertTrue(before["ready"], "runtime health overlay must not mutate cached deploy-time manifest")
        self.assertNotIn("secret://", json.dumps(revoked))
        recovered = capabilities.apply_connection_health(manifest, {"calendar": {"health": "healthy"}})
        self.assertTrue(next(x for x in recovered["skills"] if x["name"] == "book-appointment")["ready"])

    def test_optional_failed_connection_degrades_without_blocking(self):
        skill = {"metadata": {"name": "take-message"}, "spec": {"optionalCapabilities": ["tickets"]}}
        report = capabilities.skill_readiness(
            {"metadata": {"name": "rec"}, "spec": {}}, [skill],
            {"environment": "dev", "connectors": {"tickets": {"provider": "jira"}}}
        )
        desired = {"source": {"name": "rec"}, "capabilities": {"skills": report, "optional": ["tickets"]}}
        manifest = capabilities.build_manifest(
            desired, {"environment": "dev", "connectors": {"tickets": {"provider": "jira"}}}
        )
        failed = capabilities.apply_connection_health(manifest, {"tickets": {"health": "failed"}})
        current = failed["skills"][0]
        self.assertTrue(current["ready"])
        self.assertEqual(current["dependencies"][0]["status"], "degraded")
        self.assertEqual(current["dependencies"][0]["fallbackTier"], 4)

    def test_operator_skills_report_lists_disabled_and_blocked_with_connect_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            self.edit_agent(workspace, disabled=["book-appointment"])
            command = [sys.executable, str(ROOT / "scripts/client-operator.py"), "skills", str(workspace),
                       "--environment", "dev", "--agent", "receptionist"]
            disabled = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            self.assertIn("DISABLED", disabled.stdout)
            self.assertIn("connect calendar", disabled.stdout)
            self.assertNotIn("secret://", disabled.stdout)
            self.edit_agent(workspace, disabled=[])
            blocked = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(blocked.returncode, 1, blocked.stderr)
            self.assertIn("BLOCKED", blocked.stdout)


if __name__ == "__main__":
    unittest.main()
