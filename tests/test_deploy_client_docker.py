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
                "calendar": {
                    "provider": "google",
                    "token_ref": "secret://google/dev-calendar",
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

    def test_opt_in_worker_renders_a_worker_and_private_coworker_api_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("kb.search",))
            worker = {
                "enabled": True, "coworker_api_key_ref": "secret://coworker-api/dev",
                "people": {"jordan@acme.example": {"channel": "inbox"}}, "owners": {"jordan@acme.example": ["*"]},
                "mailbox": {"addresses": ["support@acme.example"], "trusted_authserv_id": "mx.acme.example", "owner": "jordan@acme.example"},
            }
            binding = self._binding(root, {"worker": worker})
            metadata = deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")
            compose = yaml.safe_load((root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
            self.assertTrue(metadata["runtimeWorker"] and metadata["coworkerApi"])
            service = compose["services"]["governed-worker"]
            self.assertEqual(service["volumes"], ["governance-data:/opt/data"])
            self.assertIn("worker.py", " ".join(service["entrypoint"]))
            self.assertEqual(service["depends_on"]["governed-tools"]["condition"], "service_healthy")
            api = compose["services"]["coworker-api"]
            self.assertEqual(api["command"], ["hermes", "gateway", "run"])
            self.assertNotIn("ports", api)
            self.assertEqual(api["volumes"], ["coworker-data:/opt/data"])
            self.assertTrue(api["environment"]["API_SERVER_KEY"].startswith("${MODEL_GARDEN_COWORKER_API_KEY:?"))
            config = json.loads((root / "bundle" / "profile" / ".modelgarden" / "worker.json").read_text(encoding="utf-8"))
            self.assertEqual(config["coworker"], {"url": "http://coworker-api:8642/v1"})
            self.assertEqual(config["mailbox"]["trustedAuthservId"], "mx.acme.example")
            self.assertNotIn("secret://", json.dumps(config))
            self.assertEqual(
                deployer.resolve_secrets(json.loads(binding.read_text()), json.dumps({
                    "secret://model/dev": "m", "secret://coworker-api/dev": "k" * 32,
                }))["secret://coworker-api/dev"], "k" * 32,
            )

    def test_opt_in_gateway_publishes_on_loopback_and_keeps_secrets_out_of_the_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("kb.search",))
            gateway = {
                "enabled": True, "public_origin": "https://chat.acme.example", "coworker_api_key_ref": "secret://coworker-api/dev",
                "secrets": {"slack_signing_secret_ref": "secret://slack/signing", "slack_bot_token_ref": "secret://slack/bot",
                            "webchat_key_ref": "secret://webchat/key"},
                "directory": {"slack": {"T01/U01": {"identity": "alice@acme.example", "roles": ["approver"]}}},
                "slack": {"bot_user_id": "U0BOT0001"},
                "web": {"allowed_addresses": ["alice@acme.example"], "mail": {"from": "chat@acme.example", "host": "smtp.acme.example"}},
                "mailbox": {"addresses": ["support@acme.example"]},
            }
            worker = {"enabled": True, "coworker_api_key_ref": "secret://coworker-api/dev"}
            binding = self._binding(root, {"gateway": gateway, "worker": worker})
            metadata = deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")
            compose = yaml.safe_load((root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
            self.assertTrue(metadata["channelGateway"])
            service = compose["services"]["channel-gateway"]
            self.assertEqual(service["ports"], ["127.0.0.1:8080:8080", "127.0.0.1:2525:2525"])
            self.assertIn("gateway.py", " ".join(service["entrypoint"]))
            self.assertTrue(service["environment"]["MODEL_GARDEN_SLACK_BOT_TOKEN"].startswith("${MODEL_GARDEN_SLACK_BOT_TOKEN:?"))
            self.assertIn("MODEL_GARDEN_SLACK_BOT_TOKEN", compose["services"]["governed-worker"]["environment"])
            config = json.loads((root / "bundle" / "profile" / ".modelgarden" / "gateway.json").read_text(encoding="utf-8"))
            self.assertEqual(config["slack"]["botUserId"], "U0BOT0001")
            self.assertEqual(config["coworker"], {"url": "http://coworker-api:8642/v1"})
            self.assertNotIn("secret://", json.dumps(config))
            self.assertTrue((root / "bundle" / "model-garden-platform" / "platform" / "channels" / "ingress.py").is_file())
            self.assertTrue((root / "bundle" / "model-garden-platform" / "platform" / "core" / "schedule_commands.py").is_file())

    def test_action_receipts_follow_the_approver_conversations_and_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("kb.search",))
            base = {"enabled": True, "public_origin": "https://chat.acme.example", "coworker_api_key_ref": "secret://coworker-api/dev",
                    "secrets": {"slack_signing_secret_ref": "secret://slack/signing", "slack_bot_token_ref": "secret://slack/bot"},
                    "directory": {"slack": {"T01/U01": {"identity": "alice@acme.example", "roles": ["approver"]}}},
                    "slack": {"bot_user_id": "U0BOT0001"}}
            for name, approvals, expected in (("a", {"notify": [{"channel": "slack", "conversation": "D0123"}]}, None),
                                              ("b", {"notify": [{"channel": "slack", "conversation": "D0123"}], "receipts": False}, False)):
                binding = self._binding(root, {"gateway": {**base, "approvals": approvals}, "worker": {"enabled": True, "coworker_api_key_ref": "secret://coworker-api/dev"}})
                deployer.render_bundle(profile, binding, root / name, "acme-dev")
                config = json.loads((root / name / "profile" / ".modelgarden" / "gateway.json").read_text(encoding="utf-8"))
                self.assertEqual(config["approvals"].get("receipts"), expected)
            self.assertTrue((root / "a" / "model-garden-platform" / "platform" / "channels" / "receipts.py").is_file())
            with self.assertRaisesRegex(ValueError, "receipts must be true or false"):
                binding = self._binding(root, {"gateway": {**base, "approvals": {"receipts": "yes"}}})
                deployer.render_bundle(profile, binding, root / "c", "acme-dev")

    def test_gateway_rejects_missing_secrets_unknown_settings_and_public_binds_that_are_not_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root)
            slack = {"enabled": True, "slack": {"bot_user_id": "U0BOT0001"}}
            with self.assertRaisesRegex(ValueError, "needs secrets"):
                deployer.render_bundle(profile, self._binding(root, {"gateway": slack}), root / "a", "acme-dev")
            with self.assertRaisesRegex(ValueError, "unknown gateway settings"):
                deployer.render_bundle(profile, self._binding(root, {"gateway": {**slack, "token": "x"}}), root / "b", "acme-dev")
            with self.assertRaisesRegex(ValueError, "IPv4"):
                deployer.render_bundle(profile, self._binding(root, {"gateway": {**slack, "bind": "all"}}), root / "c", "acme-dev")
            with self.assertRaisesRegex(ValueError, "secret://"):
                deployer.render_bundle(profile, self._binding(root, {"gateway": {**slack, "secrets": {"slack_bot_token_ref": "plain"}}}), root / "d", "acme-dev")

    def test_upstream_mcp_is_opt_in_and_injects_each_secret_into_the_sidecar_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root)
            refs = ["secret://hubspot/receptionist", "secret://hubspot/other"]
            binding = self._binding(root, {"upstream": {"mcp": {"enabled": True, "secret_refs": refs}}})
            metadata = deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")
            compose = yaml.safe_load((root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
            self.assertTrue(metadata["upstreamMcp"] and metadata["governedToolService"])
            sidecar = compose["services"]["governed-tools"]["environment"]
            self.assertEqual(sidecar["MODEL_GARDEN_UPSTREAM_MCP"], "enabled")
            secrets = [name for name in sidecar if name.startswith("MODEL_GARDEN_SECRET_")]
            self.assertEqual(len(secrets), 2)
            self.assertNotIn("MODEL_GARDEN_SECRET_", json.dumps(compose["services"]["hermes"]))
            self.assertNotIn("hubspot", (root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
            self.assertTrue((root / "bundle" / "model-garden-platform" / "platform" / "runtime" / "upstream_http.py").is_file())
            plain = deployer.render_bundle(profile, self._binding(root, {"upstream": {"mcp": {"enabled": False}}}), root / "plain", "acme-dev")
            self.assertFalse(plain["upstreamMcp"])
            for bad in ({"mcp": {"enabled": True}}, {"mcp": {"enabled": True, "secret_refs": ["plain"]}}, {"http": {}}):
                with self.assertRaisesRegex(ValueError, "upstream"):
                    deployer.render_bundle(profile, self._binding(root, {"upstream": bad}), root / f"bad-{len(str(bad))}", "acme-dev")

    def test_worker_ledger_export_is_rendered_as_non_secret_settings_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("kb.search",))
            export = {"gcs_bucket": "acme-ledger-export", "impersonate_service_account": "mg-exp-1234abcd@acme-proj.iam.gserviceaccount.com", "prefix": "prod"}
            worker = {"enabled": True, "coworker_api_key_ref": "secret://coworker-api/dev", "ledger_export": export}
            deployer.render_bundle(profile, self._binding(root, {"worker": worker}), root / "bundle", "acme-dev")
            config = json.loads((root / "bundle" / "profile" / ".modelgarden" / "worker.json").read_text(encoding="utf-8"))
            self.assertEqual(config["export"], {"gcsBucket": "acme-ledger-export", "impersonate": export["impersonate_service_account"], "prefix": "prod"})
            with self.assertRaisesRegex(ValueError, "ledger_export"):
                deployer.render_bundle(profile, self._binding(root, {"worker": {**worker, "ledger_export": {"token": "x"}}}), root / "bad", "acme-dev")

    def test_worker_and_gateway_share_people_and_escalation_and_must_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root, tools=("kb.search",))
            gateway = {
                "enabled": True, "coworker_api_key_ref": "secret://coworker-api/dev",
                "secrets": {"slack_signing_secret_ref": "secret://slack/signing", "slack_bot_token_ref": "secret://slack/bot"},
                "slack": {"bot_user_id": "U0BOT0001"},
                "people": {"jordan@acme.example": {"channel": "slack", "address": "D0123", "identity": "U123"}},
                "escalate_to": "ops@acme.example",
            }
            worker = {"enabled": True, "coworker_api_key_ref": "secret://coworker-api/dev"}
            deployer.render_bundle(profile, self._binding(root, {"gateway": gateway, "worker": worker}), root / "a", "acme-dev")
            wc = json.loads((root / "a" / "profile" / ".modelgarden" / "worker.json").read_text(encoding="utf-8"))
            self.assertEqual(wc["people"]["jordan@acme.example"]["address"], "D0123")
            self.assertEqual(wc["escalateTo"], "ops@acme.example")
            # and the other way round: the worker's list fills an omitting gateway
            gateway2 = {k: v for k, v in gateway.items() if k not in ("people", "escalate_to")}
            worker2 = {**worker, "people": {"jordan@acme.example": {"channel": "inbox"}}, "escalate_to": "boss@acme.example"}
            deployer.render_bundle(profile, self._binding(root, {"gateway": gateway2, "worker": worker2}), root / "b", "acme-dev")
            gc = json.loads((root / "b" / "profile" / ".modelgarden" / "gateway.json").read_text(encoding="utf-8"))
            self.assertEqual(gc["people"], {"jordan@acme.example": {"channel": "inbox"}})
            self.assertEqual(gc["escalateTo"], "boss@acme.example")
            # disagreement fails the deploy
            worker3 = {**worker, "people": {"jordan@acme.example": {"channel": "slack", "address": "D9999"}}}
            with self.assertRaisesRegex(ValueError, "disagree about jordan@acme.example"):
                deployer.render_bundle(profile, self._binding(root, {"gateway": gateway, "worker": worker3}), root / "c", "acme-dev")

    def test_worker_is_off_unless_enabled_and_rejects_unknown_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = self._profile(root)
            deployer.render_bundle(profile, self._binding(root, {"worker": {"enabled": False}}), root / "a", "acme-dev")
            compose = yaml.safe_load((root / "a" / "compose.yaml").read_text(encoding="utf-8"))
            self.assertNotIn("governed-worker", compose["services"])
            with self.assertRaisesRegex(ValueError, "unknown worker settings"):
                deployer.render_bundle(profile, self._binding(root, {"worker": {"enabled": True, "api_key": "x"}}), root / "b", "acme-dev")
            with self.assertRaisesRegex(ValueError, "secret://"):
                deployer.render_bundle(profile, self._binding(root, {"worker": {"enabled": True, "coworker_api_key_ref": "plain"}}), root / "c", "acme-dev")

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


class RuntimeSourceClosureTests(unittest.TestCase):
    """The sidecar image holds only RUNTIME_SOURCE_PATHS, so every runtime entry point must load from those files alone."""

    def test_runtime_entry_points_load_from_the_shipped_files_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            shipped = pathlib.Path(tmp)
            for relative in deployer.RUNTIME_SOURCE_PATHS:
                (shipped / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, shipped / relative)
            modules = ["platform/runtime/governed_mcp.py", "platform/runtime/worker.py", "platform/runtime/gateway.py"]
            for relative in modules:
                with self.subTest(module=relative):
                    loaded = subprocess.run(
                        [sys.executable, "-I", "-c", LOAD_BY_PATH, relative],
                        cwd=shipped, capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(loaded.returncode, 0, loaded.stderr[-2000:])
            for script in sorted(path for path in deployer.RUNTIME_SOURCE_PATHS if path.suffix == ".py" and path.parent.name == "scripts"):
                with self.subTest(script=str(script)):
                    shown = subprocess.run(
                        [sys.executable, "-I", str(script), "--help"], cwd=shipped, capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(shown.returncode, 0, shown.stderr[-2000:])


LOAD_BY_PATH = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("shipped_module", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["shipped_module"] = module
spec.loader.exec_module(module)
"""


if __name__ == "__main__":
    unittest.main()
