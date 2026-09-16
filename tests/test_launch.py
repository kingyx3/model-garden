from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "launch.py"
spec = importlib.util.spec_from_file_location("model_garden_launch", SCRIPT)
launch = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(launch)


class MinimalLaunchTests(unittest.TestCase):
    def test_resolve_defaults_uses_json_project_and_authenticated_github_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential = pathlib.Path(tmp) / "bootstrap.json"
            credential.write_text("{}", encoding="utf-8")
            fake_cloud = types.SimpleNamespace(
                load_service_account_key=lambda path: {
                    "project_id": "acme-project-12345",
                    "private_key_id": "key",
                    "private_key": "secret",
                    "client_email": "bootstrap@example.invalid",
                }
            )
            with mock.patch.object(launch, "_load_bootstrap_cloud", return_value=fake_cloud), mock.patch.object(
                launch, "_github_login", return_value="kingyx3"
            ):
                repo, project = launch.resolve_defaults("acme", credential, None, None)
            self.assertEqual(repo, "kingyx3/acme-ai-workspace")
            self.assertEqual(project, "acme-project-12345")

    def test_explicit_repo_and_project_do_not_require_bootstrap_key_or_github_lookup(self):
        with mock.patch.object(launch, "_load_bootstrap_cloud") as cloud, mock.patch.object(
            launch, "_github_login"
        ) as github_login, mock.patch.object(launch, "_existing_gcp_project") as existing:
            repo, project = launch.resolve_defaults(
                "acme",
                None,
                "client-org/custom-workspace",
                "client-target-12345",
            )
        cloud.assert_not_called()
        github_login.assert_not_called()
        existing.assert_not_called()
        self.assertEqual(repo, "client-org/custom-workspace")
        self.assertEqual(project, "client-target-12345")

    def test_rerun_without_deleted_bootstrap_key_reuses_recorded_gcp_project(self):
        with mock.patch.object(launch, "_github_login", return_value="kingyx3"), mock.patch.object(
            launch, "_existing_gcp_project", return_value="acme-project-12345"
        ):
            repo, project = launch.resolve_defaults("acme", None, None, None)
        self.assertEqual(repo, "kingyx3/acme-ai-workspace")
        self.assertEqual(project, "acme-project-12345")

    def test_first_launch_without_key_project_or_existing_config_fails_closed(self):
        with mock.patch.object(launch, "_github_login", return_value="kingyx3"), mock.patch.object(
            launch, "_existing_gcp_project", return_value=None
        ):
            with self.assertRaisesRegex(ValueError, "bootstrap credential is required"):
                launch.resolve_defaults("acme", None, None, None)

    def test_github_login_requires_authenticated_cli(self):
        result = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="not logged in")
        with mock.patch.object(launch.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
            launch.subprocess, "run", return_value=result
        ):
            with self.assertRaisesRegex(ValueError, "cannot determine authenticated GitHub user"):
                launch._github_login()


if __name__ == "__main__":
    unittest.main()
