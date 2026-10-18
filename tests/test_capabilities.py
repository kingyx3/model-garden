from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest

import yaml
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    module_spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


capabilities = _load("capabilities_under_test", "platform/core/capabilities.py")
compiler = _load("compile_workspace_for_capabilities", "scripts/compile-workspace.py")
deployer = _load("deploy_client_docker_for_capabilities", "scripts/deploy-client-docker.py")
runtime_module = _load("governed_mcp_for_capabilities", "platform/runtime/governed_mcp.py")

GOOGLE_BINDING = {
    "environment": "dev",
    "connectors": {
        "calendar": {
            "provider": "google",
            "token_ref": "secret://google/dev-calendar",
            "calendar_id": "frontdesk@acme-tenant.example",
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
            "owner": "office-manager@acme.example",
        },
        # The reference binding also satisfies the receptionist's knowledge and contact-log
        # dependencies at Tier 0, with no third-party account.
        "kb": {"provider": "text-knowledge", "tier": 0},
        "crm": {"provider": "built-in-crm", "tier": 0},
    },
}
TIER_ZERO_CONNECTORS = {key: GOOGLE_BINDING["connectors"][key] for key in ("kb", "crm")}


class CapabilityCatalogTests(unittest.TestCase):
    def test_catalog_matches_its_schema_and_covers_every_family(self):
        schema = json.loads((ROOT / "contracts" / "v1" / "capability-catalog.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        catalog = capabilities.load_catalog()
        self.assertEqual(sorted(Draft202012Validator(schema).iter_errors(catalog), key=str), [])
        self.assertEqual(
            sorted(catalog["families"]),
            ["calendar", "chat", "code", "crm", "docs", "files", "kb", "mail", "tickets"],
        )
        risks = {
            tool["risk"]
            for family in catalog["families"].values()
            for tool in family["tools"].values()
        }
        self.assertEqual(risks, {"read", "write", "privileged", "external-communication"})
        for family, entry in catalog["families"].items():
            for name in entry["tools"]:
                self.assertTrue(name.startswith(family + "."), name)

    def test_reference_calendar_tools_are_expressed_through_the_contract(self):
        for filename, risk in (("calendar-availability.yaml", "read"), ("calendar-book.yaml", "write")):
            tool = yaml.safe_load((ROOT / "examples" / "workspace" / "tools" / filename).read_text())
            self.assertEqual(tool["spec"]["capability"], "calendar")
            self.assertEqual(capabilities.tool_risk(tool["metadata"]["name"]), risk)
            self.assertEqual(capabilities.tool_contract_errors(tool), [])

    def test_tool_contract_rejects_risk_drift_and_unknown_tools(self):
        tool = {"metadata": {"name": "calendar.book"}, "spec": {"risk": "read", "capability": "calendar"}}
        self.assertIn("does not match catalog risk", capabilities.tool_contract_errors(tool)[0])
        tool = {"metadata": {"name": "calendar.delete-everything"}, "spec": {"risk": "write", "capability": "calendar"}}
        self.assertIn("not a portable", capabilities.tool_contract_errors(tool)[0])
        tool = {"metadata": {"name": "custom.tool"}, "spec": {"risk": "write"}}
        self.assertEqual(capabilities.tool_contract_errors(tool), [])

    def test_required_beats_optional_across_agent_and_skills(self):
        agent = {"spec": {"optionalCapabilities": ["mail"]}}
        skills = [{"spec": {"requiredCapabilities": ["mail", "calendar.book"]}}, {"spec": {"optionalCapabilities": ["kb"]}}]
        self.assertEqual(
            capabilities.collect_declarations(agent, skills),
            {"required": ["calendar.book", "mail"], "optional": ["kb"]},
        )


class CapabilityResolutionTests(unittest.TestCase):
    def _workspace(self, root: pathlib.Path) -> pathlib.Path:
        workspace = root / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", workspace)
        return workspace

    def test_workspace_without_binding_compiles_with_declarations_only(self):
        compiled = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
        self.assertEqual(compiled["capabilities"], {"optional": ["tickets"], "required": ["crm", "kb"]})

    def test_required_capability_without_binding_fails_closed_with_clear_message(self):
        with self.assertRaisesRegex(
            ValueError,
            r"requires capability 'crm' \(needed by Skill 'log-conversation'\), 'kb' \(needed by Skill 'answer-faq'\) "
            r"but environment 'dev' binds no provider",
        ):
            compiler.compile_workspace(
                ROOT / "examples" / "workspace", "receptionist", environment_binding={"environment": "dev"}
            )

    def test_unknown_declaration_fails_compilation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(pathlib.Path(tmp))
            path = workspace / "agents" / "front-office" / "receptionist" / "agent.yaml"
            agent = yaml.safe_load(path.read_text())
            agent["spec"]["optionalCapabilities"] = ["teleport"]
            path.write_text(yaml.safe_dump(agent, sort_keys=False))
            with self.assertRaisesRegex(ValueError, "unknown capability 'teleport'"):
                compiler.compile_workspace(workspace, "receptionist")

    def test_existing_workspace_without_declarations_compiles_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(pathlib.Path(tmp))
            for path in workspace.rglob("*.yaml"):
                document = yaml.safe_load(path.read_text())
                spec = document.get("spec", {})
                for key in ("requiredCapabilities", "optionalCapabilities", "capability"):
                    spec.pop(key, None)
                path.write_text(yaml.safe_dump(document, sort_keys=False))
            compiled = compiler.compile_workspace(workspace, "receptionist")[0]
            self.assertNotIn("capabilities", compiled)

    def test_cli_binding_failure_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binding = pathlib.Path(tmp) / "dev.yaml"
            binding.write_text("environment: dev\nruntime:\n  profile: receptionist\n", encoding="utf-8")
            import subprocess
            import sys
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                 "--agent", "receptionist", "--environment-binding", str(binding)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("requires capability 'crm'", result.stderr)


class SkillReadinessTests(unittest.TestCase):
    def _receptionist(self):
        resources, _, _ = compiler.load_workspace(ROOT / "examples" / "workspace")
        agent = resources["Agent"]["receptionist"]
        return agent, [resources["Skill"][name] for name in agent["spec"]["skills"]]

    def _run_cli(self, binding: dict, *extra: str):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "binding.json"
            path.write_text(json.dumps(binding), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                 "--environment-binding", str(path), *extra],
                capture_output=True, text=True,
            )

    def test_agent_declarations_get_their_own_entry(self):
        agent = {"metadata": {"name": "solo"}, "spec": {"optionalCapabilities": ["mail"]}}
        report = capabilities.skill_readiness(agent, [], {"environment": "dev"})
        self.assertEqual(report[0]["kind"], "Agent")
        self.assertEqual(report[0]["dependencies"][0]["status"], "degraded")

    def test_missing_required_capability_blocks_skill_and_error_names_it(self):
        agent = {"metadata": {"name": "receptionist"}, "spec": {}}
        skills = [
            {"metadata": {"name": "log-call"}, "spec": {"requiredCapabilities": ["mail"]}},
            {"metadata": {"name": "answer-faq"}, "spec": {}},
        ]
        report = {entry["name"]: entry for entry in capabilities.skill_readiness(agent, skills, GOOGLE_BINDING)}
        self.assertFalse(report["log-call"]["ready"])
        self.assertEqual(report["log-call"]["dependencies"][0]["status"], "missing")
        self.assertTrue(report["answer-faq"]["ready"])
        with self.assertRaisesRegex(
            ValueError,
            r"Agent 'receptionist' requires capability 'mail' \(needed by Skill 'log-call'\) but environment 'dev' binds no provider",
        ):
            capabilities.resolve(
                capabilities.collect_declarations(agent, skills),
                GOOGLE_BINDING,
                agent_name="receptionist",
                required_by=capabilities.skills_requiring(skills),
            )

class CapabilityManifestTests(unittest.TestCase):
    def _desired(self) -> dict:
        desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
        desired["source"]["client"] = "acme"
        return desired

    def test_old_profile_missing_disabled_declarations_fails_closed(self):
        desired = self._desired()
        desired["agent"]["spec"]["disabledSkills"] = ["book-appointment"]
        desired["skills"] = [
            skill for skill in desired["skills"] if skill["metadata"]["name"] != "book-appointment"
        ]
        manifest = capabilities.build_manifest(desired, {"environment": "dev"})
        booking = next(s for s in manifest["skills"] if s["name"] == "book-appointment")
        self.assertFalse(booking["enabled"])
        self.assertFalse(booking["ready"])
        self.assertFalse(booking["canEnable"])
        degraded = capabilities.apply_connection_health(manifest, {})
        booking_after = next(s for s in degraded["skills"] if s["name"] == "book-appointment")
        self.assertFalse(booking_after["canEnable"])

    def test_connection_health_rejects_unknown_family_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                capabilities.record_connection_health(pathlib.Path(tmp), "../etc", "revoked", actor="op")
            with self.assertRaises(ValueError):
                capabilities.record_connection_health(pathlib.Path(tmp), "calendar", "great", actor="op")

    def test_invalid_read_audit_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "audit.reads"):
            deployer._read_audit_mode({"spec": {"audit": {"reads": "everything"}}})


if __name__ == "__main__":
    unittest.main()
