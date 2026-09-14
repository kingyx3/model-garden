from __future__ import annotations

import importlib.util
import pathlib
import shutil
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
        for directory in ("contracts", "scripts", "examples"):
            shutil.copytree(ROOT / directory, repo / directory)
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

    def test_locked_release_supplies_selected_curated_skill_and_tool_without_manual_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            agent_path = workspace / "agents" / "receptionist" / "agent.yaml"
            agent = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
            agent["spec"]["skills"] = ["book-appointment"]
            agent["spec"]["tools"] = ["calendar.book"]
            agent_path.write_text(yaml.safe_dump(agent, sort_keys=False), encoding="utf-8")
            repository = self._release_repo(root)
            resolved = rebuild.resolve_platform_release(workspace, root / "cache", str(repository))
            profile = root / "hermes-profile"

            changed = rebuild.rebuild(workspace, "receptionist", profile, platform_root=resolved)

            skill_path = pathlib.Path("skills/model-garden/book-appointment/SKILL.md")
            self.assertIn(skill_path, changed)
            self.assertTrue((profile / skill_path).is_file())
            self.assertIn("calendar.book", (profile / skill_path).read_text(encoding="utf-8"))

    def test_missing_locked_release_tag_fails_closed_and_cleans_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = bootstrap.bootstrap(root / "acme-ai-workspace", "acme")
            repository = self._release_repo(root, "0.1.0")
            destination = root / "cache" / "v0.2.0"

            with self.assertRaisesRegex(ValueError, "cannot resolve Model Garden release v0.2.0"):
                rebuild.resolve_platform_release(workspace, root / "cache", str(repository))

            self.assertFalse(destination.exists())

    def test_missing_curated_resource_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
            (root / "platform").mkdir()
            (root / "platform" / "hermes.lock").write_text(
                (ROOT / "platform" / "hermes.lock").read_text(encoding="utf-8"), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "no curated resource root"):
                rebuild.curated_library_roots(root)

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
