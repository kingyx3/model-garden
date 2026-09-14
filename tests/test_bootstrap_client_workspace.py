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
            self.assertTrue((workspace / "environments" / "dev.yaml").is_file())
            self.assertTrue((workspace / "environments" / "prod.yaml").is_file())

            lock = yaml.safe_load((workspace / "platform.lock.yaml").read_text(encoding="utf-8"))
            self.assertEqual(lock["modelgarden"], (ROOT / "VERSION").read_text(encoding="utf-8").strip())
            hermes_lock = dict(
                line.split("=", 1)
                for line in (ROOT / "platform" / "hermes.lock").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(lock["hermes"], hermes_lock["version"])
            self.assertEqual(lock["hermes_revision"], hermes_lock["commit"])

            validate = subprocess.run(
                ["python3", str(ROOT / "scripts" / "validate-workspace.py"), str(workspace)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(validate.returncode, 0, validate.stderr)
            self.assertIn("Validated 2 Model Garden resource(s)", validate.stdout)

            desired = pathlib.Path(tmp) / "receptionist.json"
            compile_result = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "compile-workspace.py"),
                    str(workspace),
                    "--agent",
                    "receptionist",
                    "--output",
                    str(desired),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            compiled = json.loads(desired.read_text(encoding="utf-8"))
            self.assertEqual(compiled["source"]["name"], "receptionist")
            self.assertEqual(compiled["skills"], [])
            self.assertEqual(compiled["tools"], [])
            self.assertEqual(compiled["knowledge"], [])

    def test_bootstrap_refuses_nonempty_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "existing"
            workspace.mkdir()
            (workspace / "keep.txt").write_text("do not replace\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                bootstrapper.bootstrap(workspace, "acme")
            self.assertEqual((workspace / "keep.txt").read_text(encoding="utf-8"), "do not replace\n")

    def test_bootstrap_rejects_unsafe_client_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            for slug in ("../acme", "Acme", "acme/client", "-acme", "acme-"):
                with self.subTest(slug=slug):
                    with self.assertRaisesRegex(ValueError, "client slug"):
                        bootstrapper.bootstrap(pathlib.Path(tmp) / "workspace", slug)


if __name__ == "__main__":
    unittest.main()
