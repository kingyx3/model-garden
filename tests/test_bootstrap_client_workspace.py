from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap-client-workspace.py"
spec = importlib.util.spec_from_file_location("bootstrap_client_workspace", SCRIPT)
bootstrapper = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bootstrapper)


class BootstrapClientWorkspaceTests(unittest.TestCase):
    def test_bootstrap_creates_pinned_valid_compilable_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "acme-ai-workspace"
            bootstrapper.bootstrap(workspace, "acme")
            self.assertTrue((workspace / "modelgarden.yaml").is_file())
            self.assertTrue((workspace / "platform.lock.yaml").is_file())
            self.assertTrue((workspace / "agents" / "receptionist" / "agent.yaml").is_file())
            self.assertTrue((workspace / "evals" / "receptionist" / "scenarios.yaml").is_file())
            self.assertTrue((workspace / "environments" / "dev.yaml").is_file())
            self.assertTrue((workspace / "environments" / "prod.yaml").is_file())
            self.assertTrue((workspace / ".github" / "workflows" / "model-garden.yml").is_file())
            lock = yaml.safe_load((workspace / "platform.lock.yaml").read_text(encoding="utf-8"))
            self.assertEqual(lock["modelgarden"], (ROOT / "VERSION").read_text(encoding="utf-8").strip())
            hermes_lock = dict(line.split("=", 1) for line in (ROOT / "platform" / "hermes.lock").read_text(encoding="utf-8").splitlines() if "=" in line)
            self.assertEqual(lock["hermes"], hermes_lock["version"])
            self.assertEqual(lock["hermes_revision"], hermes_lock["commit"])
            dev = yaml.safe_load((workspace / "environments" / "dev.yaml").read_text(encoding="utf-8"))
            prod = yaml.safe_load((workspace / "environments" / "prod.yaml").read_text(encoding="utf-8"))
            self.assertEqual(dev["runtime"]["profile"], "receptionist")
            self.assertEqual(dev["runtime"]["model_credential_ref"], "secret://model/dev")
            self.assertEqual(prod["runtime"]["model_credential_ref"], "secret://model/prod")
            validate = subprocess.run(["python3", str(ROOT / "scripts" / "validate-workspace.py"), str(workspace)], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(validate.returncode, 0, validate.stderr)
            self.assertIn("Validated 2 Model Garden resource(s)", validate.stdout)
            desired = pathlib.Path(tmp) / "receptionist.json"
            compile_result = subprocess.run(["python3", str(ROOT / "scripts" / "compile-workspace.py"), str(workspace), "--agent", "receptionist", "--output", str(desired)], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            compiled = json.loads(desired.read_text(encoding="utf-8"))
            self.assertEqual(compiled["source"]["name"], "receptionist")
            self.assertEqual(compiled["skills"], [])
            self.assertEqual(compiled["tools"], [])
            self.assertEqual(compiled["knowledge"], [])

    def test_bootstrap_supports_generic_initial_employee_without_platform_fork(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "acme-sales-ai-workspace"
            bootstrapper.bootstrap(workspace, "acme", "sales-assistant", "Sales Assistant")
            agent = workspace / "agents" / "sales-assistant" / "agent.yaml"
            instructions = workspace / "agents" / "sales-assistant" / "instructions.md"
            self.assertTrue(agent.is_file())
            self.assertIn("# Sales Assistant", instructions.read_text(encoding="utf-8"))
            self.assertTrue((workspace / "evals" / "sales-assistant" / "scenarios.yaml").is_file())
            root_config = yaml.safe_load((workspace / "modelgarden.yaml").read_text(encoding="utf-8"))
            self.assertEqual(root_config["workspace"]["agents"], ["sales-assistant"])
            dev = yaml.safe_load((workspace / "environments" / "dev.yaml").read_text(encoding="utf-8"))
            self.assertEqual(dev["runtime"]["profile"], "sales-assistant")
            workflow = (workspace / ".github" / "workflows" / "model-garden.yml").read_text(encoding="utf-8")
            self.assertIn("--agent sales-assistant", workflow)
            self.assertNotIn("--agent receptionist", workflow)
            validate = subprocess.run(["python3", str(ROOT / "scripts" / "validate-workspace.py"), str(workspace)], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(validate.returncode, 0, validate.stderr)
            desired = pathlib.Path(tmp) / "sales-assistant.json"
            compile_result = subprocess.run(["python3", str(ROOT / "scripts" / "compile-workspace.py"), str(workspace), "--agent", "sales-assistant", "--output", str(desired)], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            compiled = json.loads(desired.read_text(encoding="utf-8"))
            self.assertEqual(compiled["source"]["name"], "sales-assistant")

    def test_bootstrap_generates_locked_github_delivery_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "acme-ai-workspace"
            bootstrapper.bootstrap(workspace, "acme")
            workflow = (workspace / ".github" / "workflows" / "model-garden.yml").read_text(encoding="utf-8")
            self.assertIn("pull_request:", workflow)
            self.assertIn("- dev", workflow)
            self.assertIn("- main", workflow)
            self.assertIn("platform.lock.yaml", workflow)
            self.assertIn('git clone --depth 1 --branch "v${version}" --single-branch', workflow)
            self.assertIn("validate-workspace.py", workflow)
            self.assertIn("rebuild-client-runtime.py", workflow)
            self.assertIn("--agent receptionist", workflow)
            self.assertIn("--dry-run", workflow)
            self.assertIn("github.ref_name == 'main' && 'prod' || 'dev'", workflow)
            self.assertIn("render-client-environment.py", workflow)
            self.assertIn("environment-binding.json", workflow)
            self.assertIn("Upload reproducible deployment inputs", workflow)
            self.assertIn("actions/upload-artifact@v4", workflow)
            self.assertIn("deploy-client-docker.py", workflow)
            self.assertIn("MODEL_GARDEN_DOCKER_RUNNER", workflow)
            self.assertIn("MODEL_GARDEN_RUNTIME_SECRETS_JSON", workflow)
            self.assertIn("--project-name \"acme-${{ github.ref_name == 'main' && 'prod' || 'dev' }}\"", workflow)
            self.assertIn("--apply", workflow)
            self.assertEqual(workflow.count("secrets."), 1)
            self.assertNotIn("secrets.OPENAI_API_KEY", workflow)
            self.assertNotIn("secrets.GOOGLE", workflow)
            self.assertNotIn("secrets.LIVEKIT", workflow)

    def test_bootstrap_refuses_nonempty_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "existing"
            workspace.mkdir()
            (workspace / "keep.txt").write_text("do not replace\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                bootstrapper.bootstrap(workspace, "acme")
            self.assertEqual((workspace / "keep.txt").read_text(encoding="utf-8"), "do not replace\n")

    def test_bootstrap_rejects_unsafe_slugs_and_role_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            for slug in ("../acme", "Acme", "acme/client", "-acme", "acme-"):
                with self.subTest(client_slug=slug):
                    with self.assertRaisesRegex(ValueError, "client slug"):
                        bootstrapper.bootstrap(pathlib.Path(tmp) / "workspace", slug)
            for slug in ("../sales", "Sales", "sales/assistant", "-sales", "sales-"):
                with self.subTest(agent_slug=slug):
                    with self.assertRaisesRegex(ValueError, "agent slug"):
                        bootstrapper.bootstrap(pathlib.Path(tmp) / "workspace", "acme", slug)
            with self.assertRaisesRegex(ValueError, "role title"):
                bootstrapper.bootstrap(pathlib.Path(tmp) / "workspace", "acme", "sales-assistant", "bad\nrole")


if __name__ == "__main__":
    unittest.main()
