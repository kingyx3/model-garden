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
        }
    },
}


class FakeCalendarTransport:
    def __init__(self, status: int = 200):
        self.status = status

    def __call__(self, method, url, headers, payload):
        if self.status != 200:
            return self.status, {"error": {"message": "token revoked"}}
        if url.endswith("/freeBusy"):
            return 200, {"calendars": {"primary": {"busy": []}}}
        return 200, {"id": "evt-1", "status": "confirmed", "htmlLink": "https://calendar.invalid/evt-1"}


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
        self.assertEqual(compiled["capabilities"], {"optional": ["tickets"], "required": ["calendar"]})

    def test_required_capability_bound_and_optional_reported_degraded(self):
        compiled = compiler.compile_workspace(
            ROOT / "examples" / "workspace", "receptionist", environment_binding=GOOGLE_BINDING
        )[0]
        resolution = compiled["capabilities"]["resolution"]
        self.assertEqual(
            resolution,
            [
                {"capability": "calendar", "family": "calendar", "required": True, "status": "bound", "provider": "google", "tier": 1},
                {"capability": "tickets", "family": "tickets", "required": False, "status": "degraded", "fallbackTier": 4},
            ],
        )
        self.assertEqual(compiled["capabilities"]["degraded"], ["tickets"])

    def test_required_capability_without_binding_fails_closed_with_clear_message(self):
        with self.assertRaisesRegex(ValueError, r"requires capability 'calendar' \(needed by Skill 'book-appointment'\) but environment 'dev' binds no provider"):
            compiler.compile_workspace(
                ROOT / "examples" / "workspace", "receptionist", environment_binding={"environment": "dev"}
            )

    def test_legacy_and_microsoft_calendar_bindings_resolve_identically(self):
        for connectors, provider in (
            ({"google_calendar": {"token_ref": "secret://g", "calendar_id": "primary"}}, "google"),
            ({"calendar": {"provider": "microsoft", "token_ref": "secret://m", "mailbox": "r@client.example"}}, "microsoft"),
        ):
            compiled = compiler.compile_workspace(
                ROOT / "examples" / "workspace",
                "receptionist",
                environment_binding={"apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding", "spec": {"environment": "prod", "connectors": connectors}},
            )[0]
            self.assertEqual(compiled["capabilities"]["resolution"][0]["provider"], provider)

    def test_ambiguous_calendar_binding_is_rejected(self):
        binding = {"connectors": {"calendar": {"provider": "google"}, "microsoft_calendar": {"mailbox": "x@y.example"}}}
        with self.assertRaisesRegex(ValueError, "more than one provider"):
            capabilities.bound_providers(binding)

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
            self.assertIn("requires capability 'calendar'", result.stderr)


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

    def test_readiness_per_skill_with_google_calendar(self):
        agent, skills = self._receptionist()
        report = {entry["name"]: entry for entry in capabilities.skill_readiness(agent, skills, GOOGLE_BINDING)}
        self.assertEqual(sorted(report), ["answer-faq", "book-appointment", "human-handoff", "take-message"])
        self.assertTrue(all(entry["kind"] == "Skill" and entry["ready"] for entry in report.values()))
        self.assertEqual(
            report["book-appointment"]["dependencies"],
            [{"capability": "calendar", "family": "calendar", "required": True, "status": "bound", "provider": "google", "tier": 1}],
        )
        self.assertEqual(
            report["take-message"]["dependencies"],
            [{"capability": "tickets", "family": "tickets", "required": False, "status": "degraded", "fallbackTier": 4}],
        )
        self.assertEqual(report["answer-faq"]["dependencies"], [])
        rendered = json.dumps(report)
        for forbidden in ("token_ref", "secret://", "calendar_id", "frontdesk@acme-tenant.example", "scopes", "owner"):
            self.assertNotIn(forbidden, rendered)

    def test_compiled_output_carries_readiness(self):
        compiled = compiler.compile_workspace(
            ROOT / "examples" / "workspace", "receptionist", environment_binding=GOOGLE_BINDING
        )[0]
        names = [entry["name"] for entry in compiled["capabilities"]["skills"]]
        self.assertEqual(names, ["answer-faq", "take-message", "human-handoff", "book-appointment"])
        self.assertNotIn("calendar_id", json.dumps(compiled["capabilities"]))

    def test_agent_declarations_get_their_own_entry(self):
        agent = {"metadata": {"name": "solo"}, "spec": {"optionalCapabilities": ["mail"]}}
        report = capabilities.skill_readiness(agent, [], {"environment": "dev"})
        self.assertEqual(report[0]["kind"], "Agent")
        self.assertEqual(report[0]["dependencies"][0]["status"], "degraded")

    def test_missing_required_capability_blocks_skill_and_error_names_it(self):
        agent = {"metadata": {"name": "receptionist"}, "spec": {}}
        skills = [
            {"metadata": {"name": "log-call"}, "spec": {"requiredCapabilities": ["crm"]}},
            {"metadata": {"name": "answer-faq"}, "spec": {}},
        ]
        report = {entry["name"]: entry for entry in capabilities.skill_readiness(agent, skills, GOOGLE_BINDING)}
        self.assertFalse(report["log-call"]["ready"])
        self.assertEqual(report["log-call"]["dependencies"][0]["status"], "missing")
        self.assertTrue(report["answer-faq"]["ready"])
        with self.assertRaisesRegex(
            ValueError,
            r"Agent 'receptionist' requires capability 'crm' \(needed by Skill 'log-call'\) but environment 'dev' binds no provider",
        ):
            capabilities.resolve(
                capabilities.collect_declarations(agent, skills),
                GOOGLE_BINDING,
                agent_name="receptionist",
                required_by=capabilities.skills_requiring(skills),
            )

    def test_readiness_cli_prints_table_and_exits_by_blocked_state(self):
        result = self._run_cli(GOOGLE_BINDING, "--agent", "receptionist", "--readiness")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("book-appointment", result.stdout)
        self.assertIn("calendar: google tier 1", result.stdout)
        self.assertIn("tickets (optional): degraded -> fallback tier 4", result.stdout)
        self.assertNotIn("frontdesk@acme-tenant.example", result.stdout)

        blocked = self._run_cli({"environment": "dev"}, "--agent", "receptionist", "--readiness")
        self.assertEqual(blocked.returncode, 1)
        self.assertRegex(blocked.stdout, r"book-appointment\s+BLOCKED\s+calendar: MISSING")


class CapabilityManifestTests(unittest.TestCase):
    def _desired(self) -> dict:
        desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
        desired["source"]["client"] = "acme"
        return desired

    def test_manifest_is_truthful_and_secret_free(self):
        manifest = capabilities.build_manifest(self._desired(), GOOGLE_BINDING)
        by_family = {entry["family"]: entry for entry in manifest["capabilities"]}
        self.assertEqual(manifest["client"], "acme")
        self.assertEqual(manifest["environment"], "dev")
        self.assertEqual(by_family["calendar"]["status"], "available")
        self.assertEqual(by_family["calendar"]["provider"], "google")
        self.assertEqual(by_family["calendar"]["tier"], 1)
        self.assertEqual(by_family["calendar"]["scopes"], ["https://www.googleapis.com/auth/calendar.events"])
        self.assertEqual(by_family["calendar"]["connectionOwner"], "office-manager@acme.example")
        self.assertEqual(by_family["calendar"]["tools"], ["calendar.availability", "calendar.book"])
        self.assertEqual(by_family["tickets"]["status"], "unbound")
        self.assertEqual(by_family["tickets"]["fallback"], "human-handoff")
        self.assertTrue(by_family["tickets"]["optional"])
        rendered = json.dumps(manifest)
        for forbidden in ("secret://", "token", "frontdesk@acme-tenant.example", "calendar_id"):
            self.assertNotIn(forbidden, rendered)

    def test_revoked_connection_becomes_unavailable_with_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            capabilities.record_connection_health(root, "calendar", "revoked", actor="operator@acme.example", reason="consent withdrawn")
            manifest = capabilities.apply_connection_health(
                capabilities.build_manifest(self._desired(), GOOGLE_BINDING),
                capabilities.read_connection_health(root),
            )
        calendar = next(entry for entry in manifest["capabilities"] if entry["family"] == "calendar")
        self.assertEqual(calendar["status"], "unavailable")
        self.assertEqual(calendar["health"], "revoked")
        self.assertEqual(calendar["fallback"], "human-handoff")

    def test_connection_health_rejects_unknown_family_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                capabilities.record_connection_health(pathlib.Path(tmp), "../etc", "revoked", actor="op")
            with self.assertRaises(ValueError):
                capabilities.record_connection_health(pathlib.Path(tmp), "calendar", "great", actor="op")

    def _runtime(self, root: pathlib.Path, transport, manifest: pathlib.Path | None = None):
        desired_path = root / "desired.json"
        desired_path.write_text(json.dumps(self._desired()), encoding="utf-8")
        environ = {"MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN": "calendar-secret"}
        if manifest is not None:
            environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(manifest)
        return runtime_module.GovernedToolRuntime(
            desired_path,
            root / "governance" / "audit.jsonl",
            root / "governance" / "approvals",
            environ=environ,
            calendar_transport=transport,
        )

    def test_runtime_tool_reflects_revocation_without_redeploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            manifest_path = root / "capability-manifest.json"
            manifest_path.write_text(json.dumps(capabilities.build_manifest(self._desired(), GOOGLE_BINDING)))
            runtime = self._runtime(root, FakeCalendarTransport(), manifest_path)
            before = {entry["family"]: entry for entry in runtime.capabilities()["capabilities"]}
            self.assertEqual(before["calendar"]["status"], "available")

            capabilities.record_connection_health(root / "governance", "calendar", "revoked", actor="operator")
            after = {entry["family"]: entry for entry in runtime.capabilities()["capabilities"]}
            self.assertEqual(after["calendar"]["status"], "unavailable")
            self.assertNotIn("calendar-secret", json.dumps(after))

    def test_runtime_records_provider_auth_failure_then_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            transport = FakeCalendarTransport(status=401)
            runtime = self._runtime(root, transport)
            with self.assertRaises(RuntimeError):
                runtime.calendar_availability(start="2026-09-16T10:00:00+08:00", end="2026-09-16T10:30:00+08:00")
            calendar = next(e for e in runtime.capabilities()["capabilities"] if e["family"] == "calendar")
            self.assertEqual((calendar["status"], calendar["health"]), ("unavailable", "failed"))

            transport.status = 200
            # Auth failures no longer recover merely because a retry would succeed.
            # A named operator must record reauthorization before retry is allowed.
            with self.assertRaisesRegex(PermissionError, "connection failed"):
                runtime.calendar_availability(start="2026-09-16T10:00:00+08:00", end="2026-09-16T10:30:00+08:00")
            capabilities.record_connection_health(
                root / "governance", "calendar", "healthy",
                actor="operator", reason="reauthorized after token renewal",
            )
            runtime.calendar_availability(start="2026-09-16T10:00:00+08:00", end="2026-09-16T10:30:00+08:00")
            calendar = next(e for e in runtime.capabilities()["capabilities"] if e["family"] == "calendar")
            self.assertEqual((calendar["status"], calendar["health"]), ("available", "healthy"))

    def test_deployment_writes_manifest_and_fails_closed_on_unbound_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = root / "profile"
            (profile / ".modelgarden").mkdir(parents=True)
            (profile / "SOUL.md").write_text("# Receptionist\n", encoding="utf-8")
            (profile / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "openai", "default": "m"}}), encoding="utf-8")
            (profile / ".modelgarden" / "desired-state.json").write_text(json.dumps(self._desired()), encoding="utf-8")
            spec = {"environment": "dev", "runtime": {"profile": "receptionist", "model_credential_ref": "secret://model/dev"}}
            unbound = root / "unbound.json"
            unbound.write_text(json.dumps({"apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding", "spec": spec}))
            with self.assertRaisesRegex(ValueError, "Calendar Tools require"):
                deployer.render_bundle(profile, unbound, root / "bundle-a", "acme-dev")
            with self.assertRaisesRegex(ValueError, "requires capability 'calendar'"):
                deployer._capability_manifest(self._desired(), json.loads(unbound.read_text()))

            bound = root / "bound.json"
            bound.write_text(json.dumps({
                "apiVersion": "modelgarden.ai/v1",
                "kind": "EnvironmentBinding",
                "spec": {**spec, "connectors": GOOGLE_BINDING["connectors"], "audit": {"reads": "sampled:10"}},
            }))
            metadata = deployer.render_bundle(profile, bound, root / "bundle-b", "acme-dev")
            manifest_path = root / "bundle-b" / "profile" / ".modelgarden" / "capability-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            compose = yaml.safe_load((root / "bundle-b" / "compose.yaml").read_text())
            tools_env = compose["services"]["governed-tools"]["environment"]
            self.assertEqual(manifest["kind"], "CapabilityManifest")
            self.assertEqual(metadata["unboundCapabilities"], ["tickets"])
            self.assertEqual(len(metadata["capabilityManifestSha256"]), 64)
            self.assertEqual(tools_env["MODEL_GARDEN_CAPABILITY_MANIFEST"], "/opt/profile-seed/.modelgarden/capability-manifest.json")
            self.assertEqual(tools_env["MODEL_GARDEN_READ_AUDIT"], "sampled:10")
            self.assertNotIn("secret://", manifest_path.read_text())
            platform_copy = root / "bundle-b" / "model-garden-platform"
            for relative in ("platform/core/capabilities.py", "contracts/v1/capabilities.json", "platform/channels/text_channel.py", "VERSION"):
                self.assertTrue((platform_copy / relative).is_file(), relative)

    def test_invalid_read_audit_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "audit.reads"):
            deployer._read_audit_mode({"spec": {"audit": {"reads": "everything"}}})


if __name__ == "__main__":
    unittest.main()
