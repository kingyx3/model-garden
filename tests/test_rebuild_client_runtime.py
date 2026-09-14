from __future__ import annotations

import importlib.util
import pathlib
import subprocess
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
    def _release_repo(self, root: pathlib.Path, version: str = "0.2.0") -> pathlib.Path:
        repo = root / "platform-source"
        (repo / "platform").mkdir(parents=True)
        (repo / "VERSION").write_text(version + "\n", encoding="utf-8")
        (repo / "platform" / "hermes.lock").write_text(
            (ROOT / "platform" / "hermes.lock").read_text(encoding="utf-8"), encoding="utf-8"
        )
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "tests@modelgarden.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Model Garden Tests"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "release fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "tag", f"v{version}"], check=True)
        return repo

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

    def test_resolves_exact_modelgarden_version_tag_from_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            repository = self._release_repo(root)

            resolved = rebuild.resolve_platform_release(workspace, root / "cache", str(repository))

            self.assertEqual(resolved, root / "cache" / "v0.2.0")
            self.assertEqual((resolved / "VERSION").read_text(encoding="utf-8").strip(), "0.2.0")
            self.assertEqual(rebuild.verify_lock(workspace, resolved)["modelgarden"], "0.2.0")

    def test_missing_locked_release_tag_fails_closed_and_cleans_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            repository = self._release_repo(root, "0.1.0")
            destination = root / "cache" / "v0.2.0"

            with self.assertRaisesRegex(ValueError, "cannot resolve Model Garden release v0.2.0"):
                rebuild.resolve_platform_release(workspace, root / "cache", str(repository))

            self.assertFalse(destination.exists())

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
