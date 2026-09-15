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
    def _profile(self, root: pathlib.Path, provider: str = "openai") -> pathlib.Path:
        profile = root / "profile"
        (profile / ".modelgarden").mkdir(parents=True)
        (profile / "skills" / "model-garden" / "answer-faq").mkdir(parents=True)
        (profile / "SOUL.md").write_text("# Receptionist\n", encoding="utf-8")
        (profile / "config.yaml").write_text(
            yaml.safe_dump({"model": {"provider": provider, "default": "approved-model"}}, sort_keys=False),
            encoding="utf-8",
        )
        (profile / ".modelgarden" / "desired-state.json").write_text("{}\n", encoding="utf-8")
        (profile / "skills" / "model-garden" / "answer-faq" / "SKILL.md").write_text(
            "---\nname: answer-faq\n---\nAnswer approved FAQs.\n", encoding="utf-8"
        )
        return profile

    def _binding(self, root: pathlib.Path, extra: dict | None = None) -> pathlib.Path:
        spec = {
            "environment": "dev",
            "runtime": {
                "profile": "receptionist",
                "model_credential_ref": "secret://model/dev",
            },
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
            compose = (bundle / "compose.yaml").read_text(encoding="utf-8")
            self.assertIn(hermes_lock["commit"], dockerfile)
            self.assertIn(hermes_lock["version"], dockerfile)
            self.assertIn("@sha256:", dockerfile)
            self.assertIn("OPENAI_API_KEY", compose)
            self.assertIn("MODEL_GARDEN_MODEL_CREDENTIAL", compose)
            self.assertNotIn("MODEL_GARDEN_RUNTIME_SECRETS_JSON", compose)
            self.assertEqual(metadata["modelCredentialRef"], "secret://model/dev")
            self.assertFalse((bundle / "hermes-source").exists())

            all_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in bundle.rglob("*")
                if path.is_file()
            )
            self.assertNotIn("super-secret-value", all_text)

    def test_secret_resolution_requires_exact_reference_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binding_path = self._binding(
                root,
                {"calendar": {"credential_ref": "secret://google/dev-calendar"}},
            )
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            exact = json.dumps({
                "secret://model/dev": "model-value",
                "secret://google/dev-calendar": "calendar-value",
            })
            resolved = deployer.resolve_secrets(binding, exact)
            self.assertEqual(resolved["secret://model/dev"], "model-value")

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


if __name__ == "__main__":
    unittest.main()
