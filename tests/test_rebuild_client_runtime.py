from __future__ import annotations

import importlib.util
import pathlib
import shutil
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "rebuild-client-runtime.py"
spec = importlib.util.spec_from_file_location("rebuild_client_runtime", MODULE_PATH)
rebuild = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(rebuild)

BOOTSTRAP_PATH = ROOT / "scripts" / "bootstrap-client-workspace.py"
bootstrap_spec = importlib.util.spec_from_file_location("bootstrap_client_workspace_for_rebuild", BOOTSTRAP_PATH)
bootstrap = importlib.util.module_from_spec(bootstrap_spec)
assert bootstrap_spec.loader is not None
bootstrap_spec.loader.exec_module(bootstrap)


class RebuildClientRuntimeTests(unittest.TestCase):
    def test_bootstrapped_workspace_rebuilds_disposable_hermes_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            profile = root / "hermes-profile"

            changed = rebuild.rebuild(workspace, "receptionist", profile)

            self.assertIn(pathlib.Path("SOUL.md"), changed)
            self.assertIn(pathlib.Path("config.yaml"), changed)
            self.assertIn(pathlib.Path(".modelgarden/desired-state.json"), changed)
            self.assertTrue((profile / "SOUL.md").is_file())
            self.assertTrue((profile / ".modelgarden" / "desired-state.json").is_file())
            self.assertFalse((profile / ".env").exists())
            self.assertEqual(rebuild.rebuild(workspace, "receptionist", profile), [])

    def test_modelgarden_version_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            lock_path = workspace / "platform.lock.yaml"
            lock = yaml.safe_load(lock_path.read_text())
            lock["modelgarden"] = "999.0.0"
            lock_path.write_text(yaml.safe_dump(lock, sort_keys=False))

            with self.assertRaisesRegex(ValueError, "lock mismatch for modelgarden"):
                rebuild.verify_lock(workspace)

    def test_hermes_revision_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            lock_path = workspace / "platform.lock.yaml"
            lock = yaml.safe_load(lock_path.read_text())
            lock["hermes_revision"] = "deadbeef"
            lock_path.write_text(yaml.safe_dump(lock, sort_keys=False))

            with self.assertRaisesRegex(ValueError, "lock mismatch for hermes_revision"):
                rebuild.verify_lock(workspace)

    def test_missing_lockfile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            workspace.mkdir()
            with self.assertRaisesRegex(ValueError, "missing lockfile"):
                rebuild.verify_lock(workspace)

    def test_dry_run_does_not_materialize_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            profile = root / "hermes-profile"

            changed = rebuild.rebuild(workspace, "receptionist", profile, dry_run=True)

            self.assertTrue(changed)
            self.assertFalse(profile.exists())


if __name__ == "__main__":
    unittest.main()
