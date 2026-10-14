from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-client-docker.py"
spec = importlib.util.spec_from_file_location("deploy_client_docker", SCRIPT)
deployer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(deployer)


class DeployClientDockerTests(unittest.TestCase):
    def _profile(
        self,
        root: pathlib.Path,
        provider: str = "openai",
        tools: tuple[str, ...] = (),
    ) -> pathlib.Path:
        profile = root / "profile"
        (profile / ".modelgarden").mkdir(parents=True)
        (profile / "skills" / "model-garden" / "answer-faq").mkdir(parents=True)
        (profile / "SOUL.md").write_text("# Receptionist\n", encoding="utf-8")
        config = {"model": {"provider": provider, "default": "approved-model"}}
        if tools:
            config["mcp_servers"] = {
                "model_garden": {
                    "url": "http://governed-tools:9120/mcp",
                    "enabled": True,
                }
            }
        (profile / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        desired = {
            "schemaVersion": 1,
            "tools": [
                {"metadata": {"name": name}, "spec": {"type": "internal", "risk": "read"}}
                for name in tools
            ],
        }
        (profile / ".modelgarden" / "desired-state.json").write_text(
            json.dumps(desired) + "\n", encoding="utf-8"
        )
        (profile / "skills" / "model-garden" / "answer-faq" / "SKILL.md").write_text(
            "---\nname: answer-faq\n---\nAnswer approved FAQs.\n", encoding="utf-8"
        )
        return profile

    def _binding(
        self,
        root: pathlib.Path,
        extra: dict | None = None,
        *,
        calendar: bool = False,
    ) -> pathlib.Path:
        spec = {
            "environment": "dev",
            "runtime": {
                "profile": "receptionist",
                "model_credential_ref": "secret://model/dev",
            },
        }
        if calendar:
            spec["connectors"] = {
                "google_calendar": {
                    "token_ref": "secret://google/dev-calendar",
                    "calendar_id": "primary",
                }
            }
        if extra:
            spec.update(extra)
        path = root / "binding.json"
        path.write_text(
            json.dumps({"apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding", "spec": spec}),
            encoding="utf-8",
        )
        return path

    def test_renders_secret_free_pinned_docker_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root)
            binding = self._binding(root)
            bundle = root / "bundle"
            metadata = deployer.render_bundle(profile, binding, bundle, "acme-dev")

            hermes_lock = dict(
                line.split("=", 1)
                for line in (ROOT / "platform" / "hermes.lock").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            dockerfile = (bundle / "Dockerfile").read_text(encoding="utf-8")
            compose = yaml.safe_load((bundle / "compose.yaml").read_text(encoding="utf-8"))
            self.assertIn(hermes_lock["commit"], dockerfile)
            self.assertIn(hermes_lock["version"], dockerfile)
            self.assertIn("@sha256:", dockerfile)
            self.assertIn("requirements-runtime.txt", dockerfile)
            self.assertIn("OPENAI_API_KEY", compose["services"]["hermes"]["environment"])
            self.assertNotIn("governed-tools", compose["services"])
            self.assertEqual(metadata["modelCredentialRef"], "secret://model/dev")
            self.assertEqual(metadata["selectedTools"], [])
            self.assertFalse(metadata["governedToolService"])
            self.assertFalse((bundle / "hermes-source").exists())
            self.assertTrue((bundle / "model-garden-platform" / "platform" / "runtime" / "governed_mcp.py").is_file())

            all_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in bundle.rglob("*")
                if path.is_file()
            )
            self.assertNotIn("super-secret-value", all_text)

    def test_calendar_tools_render_isolated_governed_mcp_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("calendar.availability", "calendar.book"))
            binding = self._binding(root, calendar=True)
            bundle = root / "bundle"

            metadata = deployer.render_bundle(profile, binding, bundle, "acme-dev")
            compose = yaml.safe_load((bundle / "compose.yaml").read_text(encoding="utf-8"))
            hermes = compose["services"]["hermes"]
            tools = compose["services"]["governed-tools"]

            self.assertEqual(metadata["selectedTools"], ["calendar.availability", "calendar.book"])
            self.assertTrue(metadata["governedToolService"])
            self.assertEqual(metadata["googleCalendarCredentialRef"], "secret://google/dev-calendar")
            self.assertIn("OPENAI_API_KEY", hermes["environment"])
            self.assertNotIn("MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN", hermes["environment"])
            self.assertIn("MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN", tools["environment"])
            self.assertNotIn("OPENAI_API_KEY", tools["environment"])
            self.assertEqual(tools["environment"]["MODEL_GARDEN_GOOGLE_CALENDAR_ID"], "primary")
            self.assertIn("governed-tools", hermes["depends_on"])
            self.assertNotIn("ports", tools)
            self.assertNotIn("MODEL_GARDEN_RUNTIME_SECRETS_JSON", json.dumps(compose))

    def test_calendar_tools_require_explicit_environment_connector_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("calendar.availability",))
            binding = self._binding(root)
            with self.assertRaisesRegex(ValueError, "connectors.google_calendar"):
                deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")

    def test_secret_resolution_requires_exact_reference_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binding_path = self._binding(root, calendar=True)
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            exact = json.dumps({
                "secret://model/dev": "model-value",
                "secret://google/dev-calendar": "calendar-value",
            })
            resolved = deployer.resolve_secrets(binding, exact)
            self.assertEqual(resolved["secret://model/dev"], "model-value")
            self.assertEqual(resolved["secret://google/dev-calendar"], "calendar-value")

            with self.assertRaisesRegex(ValueError, "unresolved secret references"):
                deployer.resolve_secrets(binding, json.dumps({"secret://model/dev": "model-value"}))
            with self.assertRaisesRegex(ValueError, "unreferenced keys"):
                deployer.resolve_secrets(
                    binding,
                    json.dumps({
                        "secret://model/dev": "model-value",
                        "secret://google/dev-calendar": "calendar-value",
                        "secret://unused": "too-broad",
                    }),
                )

    def test_requires_model_credential_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root)
            binding = root / "binding.json"
            binding.write_text(json.dumps({
                "apiVersion": "modelgarden.ai/v1",
                "kind": "EnvironmentBinding",
                "spec": {"environment": "dev", "runtime": {"profile": "receptionist"}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "model_credential_ref"):
                deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")

    def test_rejects_unsupported_model_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, "internal")
            binding = self._binding(root)
            with self.assertRaisesRegex(ValueError, "no approved MVP credential mapping"):
                deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")

    def test_refuses_unsafe_or_nonempty_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root)
            binding = self._binding(root)
            with self.assertRaisesRegex(ValueError, "project name"):
                deployer.render_bundle(profile, binding, root / "bundle", "../acme")

            bundle = root / "existing"
            bundle.mkdir()
            (bundle / "keep").write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                deployer.render_bundle(profile, binding, bundle, "acme-dev")


    def test_microsoft_graph_calendar_uses_same_tools_with_isolated_provider_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("calendar.availability", "calendar.book"))
            binding = self._binding(root, extra={
                "connectors": {"calendar": {
                    "provider": "microsoft",
                    "token_ref": "secret://microsoft/dev-calendar",
                    "mailbox": "reception@example.com",
                }}
            })
            bundle = root / "bundle"
            metadata = deployer.render_bundle(profile, binding, bundle, "acme-dev")
            compose = yaml.safe_load((bundle / "compose.yaml").read_text(encoding="utf-8"))
            hermes = compose["services"]["hermes"]["environment"]
            sidecar = compose["services"]["governed-tools"]["environment"]
            self.assertEqual(metadata["calendarProvider"], "microsoft")
            self.assertEqual(metadata["calendarCredentialRef"], "secret://microsoft/dev-calendar")
            self.assertEqual(sidecar["MODEL_GARDEN_CALENDAR_PROVIDER"], "microsoft")
            self.assertEqual(sidecar["MODEL_GARDEN_MICROSOFT_CALENDAR_MAILBOX"], "reception@example.com")
            self.assertIn("MODEL_GARDEN_MICROSOFT_CALENDAR_TOKEN", sidecar)
            self.assertNotIn("MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN", sidecar)
            self.assertNotIn("MODEL_GARDEN_MICROSOFT_CALENDAR_TOKEN", hermes)
            self.assertNotIn("OPENAI_API_KEY", sidecar)
            self.assertTrue((bundle / "model-garden-platform" / "platform" / "connectors" / "microsoft_calendar.py").is_file())
            self.assertEqual(
                deployer.resolve_secrets(json.loads(binding.read_text(encoding="utf-8")), json.dumps({
                    "secret://model/dev": "model", "secret://microsoft/dev-calendar": "graph"
                }))["secret://microsoft/dev-calendar"], "graph",
            )

    def test_calendar_configuration_rejects_ambiguous_or_unapproved_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("calendar.availability",))
            binding = self._binding(root, extra={
                "connectors": {
                    "google_calendar": {"token_ref": "secret://google/dev-calendar"},
                    "calendar": {"provider": "microsoft", "token_ref": "secret://ms/dev", "mailbox": "staff@example.com"},
                }
            })
            with self.assertRaisesRegex(ValueError, "exactly one"):
                deployer.render_bundle(profile, binding, root / "ambiguous", "acme-dev")
            binding = self._binding(root, extra={"connectors": {
                "calendar": {"provider": "unapproved", "token_ref": "secret://other/dev"}
            }})
            with self.assertRaisesRegex(ValueError, "provider must be google or microsoft"):
                deployer.render_bundle(profile, binding, root / "unapproved", "acme-dev")


if __name__ == "__main__":
    unittest.main()
