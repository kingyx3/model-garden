from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "model-garden-client.py"
spec = importlib.util.spec_from_file_location("model_garden_client", SCRIPT)
client_cli = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(client_cli)


class ModelGardenClientTests(unittest.TestCase):
    def test_init_defaults_to_backward_compatible_receptionist(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "acme-ai-workspace"
            client_cli.init_client(workspace, "acme")
            self.assertTrue((workspace / "agents" / "receptionist" / "agent.yaml").is_file())

    def test_init_supports_generic_role_and_locked_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "acme-ai-workspace"
            client_cli.init_client(workspace, "acme", "sales-assistant")

            agent_path = workspace / "agents" / "sales-assistant" / "agent.yaml"
            self.assertTrue(agent_path.is_file())
            self.assertFalse((workspace / "agents" / "receptionist").exists())
            agent = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
            self.assertEqual(agent["metadata"]["name"], "sales-assistant")
            self.assertEqual(agent["spec"]["skills"], [])
            self.assertEqual(agent["spec"]["tools"], [])

            manifest = yaml.safe_load((workspace / "modelgarden.yaml").read_text(encoding="utf-8"))
            self.assertEqual(manifest["workspace"]["agents"], ["sales-assistant"])
            for environment in ("dev", "prod"):
                binding = yaml.safe_load((workspace / "environments" / f"{environment}.yaml").read_text(encoding="utf-8"))
                self.assertEqual(binding["runtime"]["profile"], "sales-assistant")

            workflow = (workspace / ".github" / "workflows" / "model-garden.yml").read_text(encoding="utf-8")
            self.assertIn("--agent sales-assistant", workflow)
            self.assertNotIn("--agent receptionist", workflow)
            self.assertTrue((workspace / "evals" / "sales-assistant" / "calls.yaml").is_file())

            validate = subprocess.run(
                ["python3", str(ROOT / "scripts" / "validate-workspace.py"), str(workspace)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(validate.returncode, 0, validate.stderr)

    def test_init_rejects_unsafe_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "role"):
                client_cli.init_client(pathlib.Path(tmp) / "workspace", "acme", "Sales/Assistant")


if __name__ == "__main__":
    unittest.main()
