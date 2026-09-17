from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "launch-client.py"
TEST_RELEASE = "9.9.9"
spec = importlib.util.spec_from_file_location("launch_client", SCRIPT)
launch = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(launch)


class LaunchClientTests(unittest.TestCase):
    def ready_variables(self, *, verified: str = "false", ownership: str = "model-garden") -> dict[str, str]:
        return {
            "MODEL_GARDEN_CLOUD_PROVIDER": "gcp",
            "MODEL_GARDEN_CLOUD_READY": "true",
            "MODEL_GARDEN_INFRA_OWNERSHIP": ownership,
            "MODEL_GARDEN_TF_STATE_BUCKET": "bucket",
            "GCP_PROJECT_ID": "acme-project-12345",
            "GCP_REGION": "asia-southeast1",
            "GCP_ZONE": "asia-southeast1-b",
            "GCP_WORKLOAD_IDENTITY_PROVIDER": "provider",
            "GCP_DEPLOY_SERVICE_ACCOUNT": "deploy@example.invalid",
            "GCP_RUNTIME_SERVICE_ACCOUNT": "runtime@example.invalid",
            launch.VERIFIED_VARIABLE: verified,
        }

    def test_cloud_ready_requires_complete_keyless_contract(self):
        values = self.ready_variables()
        self.assertTrue(launch._cloud_ready(values))
        values.pop("GCP_RUNTIME_SERVICE_ACCOUNT")
        self.assertFalse(launch._cloud_ready(values))

        values = self.ready_variables()
        values.pop("MODEL_GARDEN_INFRA_OWNERSHIP")
        self.assertFalse(launch._cloud_ready(values))

    def test_cloud_mismatch_is_reported_instead_of_silently_reusing_other_target(self):
        values = self.ready_variables(ownership="client")
        values["GCP_PROJECT_ID"] = "other-project-12345"
        mismatches = launch._cloud_mismatches(
            values,
            project_id="acme-project-12345",
            region="asia-southeast1",
            zone="asia-southeast1-b",
            ownership="client",
        )
        self.assertEqual(len(mismatches), 1)
        self.assertIn("GCP_PROJECT_ID", mismatches[0])
        self.assertIn("other-project-12345", mismatches[0])

    def test_existing_workspace_must_match_client_and_requested_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp)
            (workspace / "modelgarden.yaml").write_text("workspace:\n  client: acme\n", encoding="utf-8")
            (workspace / "platform.lock.yaml").write_text(f"modelgarden: {TEST_RELEASE}\n", encoding="utf-8")

            def origin_runner(command, *, cwd=None, **kwargs):
                self.assertEqual(cwd, workspace)
                return subprocess.CompletedProcess(command, 0, stdout="git@github.com:kingyx3/acme-ai-workspace.git\n", stderr="")

            launch._validate_workspace_identity(workspace, "acme", "kingyx3/acme-ai-workspace", origin_runner)
            with self.assertRaisesRegex(ValueError, "cross-client launch"):
                launch._validate_workspace_identity(workspace, "other", "kingyx3/acme-ai-workspace", origin_runner)
            with self.assertRaisesRegex(ValueError, "cross-repository launch"):
                launch._validate_workspace_identity(workspace, "acme", "kingyx3/other-ai-workspace", origin_runner)

    def test_public_client_repo_is_rejected_before_configuration(self):
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"nameWithOwner": "kingyx3/acme-ai-workspace", "isPrivate": False}),
                stderr="",
            )
        )
        with self.assertRaisesRegex(ValueError, "must be private"):
            launch._require_private_repo("kingyx3/acme-ai-workspace", runner)

    def test_repository_variable_and_run_reads_fail_closed(self):
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="auth failed"))
        with self.assertRaisesRegex(ValueError, "repository variables"):
            launch._repo_variables("kingyx3/acme-ai-workspace", failed)
        with self.assertRaisesRegex(ValueError, "deployment runs"):
            launch._run_ids("kingyx3/acme-ai-workspace", failed)

    def test_runtime_secret_setup_skips_existing_environments_on_resume(self):
        calls = []
        with mock.patch.object(launch, "_runtime_secret_present", return_value=True), mock.patch.object(
            launch, "_run_operator", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))
        ):
            launch._ensure_runtime_secrets(pathlib.Path("workspace"), "kingyx3/acme-ai-workspace", runner=mock.Mock(), rotate=False)
        self.assertEqual(calls, [])

    def test_runtime_secret_rotation_reconfigures_both_environments(self):
        calls: list[list[str]] = []

        def capture(args, *, runner, capture=False):
            calls.append(args)

        with mock.patch.object(launch, "_run_operator", side_effect=capture):
            launch._ensure_runtime_secrets(pathlib.Path("workspace"), "kingyx3/acme-ai-workspace", runner=mock.Mock(), rotate=True)
        self.assertEqual([call[-1] for call in calls], ["dev", "prod"])
        self.assertTrue(all(call[0] == "configure" for call in calls))

    def test_wait_for_new_cloud_run_watches_only_new_run(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        with mock.patch.object(launch, "_run_ids", side_effect=[{1}, {1, 2}]):
            run_id = launch._wait_for_new_cloud_run(
                "kingyx3/acme-ai-workspace", {1}, runner=runner, poll_seconds=0, attempts=2
            )
        self.assertEqual(run_id, 2)
        watch = runner.call_args.args[0]
        self.assertEqual(watch[:4], ["gh", "run", "watch", "2"])
        self.assertIn("--exit-status", watch)

    def test_revoke_uses_isolated_gcloud_config_and_deletes_json_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential = pathlib.Path(tmp) / "bootstrap.json"
            credential.write_text("{}", encoding="utf-8")
            fake_cloud = types.SimpleNamespace(
                load_service_account_key=lambda path: {
                    "project_id": "bootstrap-project-12345",
                    "private_key_id": "key-123",
                    "private_key": "secret",
                    "client_email": "bootstrap@example.iam.gserviceaccount.com",
                }
            )
            seen_envs = []

            def fake_run(command, *, cwd=None, env=None, input_text=None, capture=True):
                seen_envs.append(dict(env or {}))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(launch, "_load_script", return_value=fake_cloud), mock.patch.object(
                launch.shutil, "which", return_value="/usr/bin/gcloud"
            ):
                self.assertTrue(launch._revoke_bootstrap_key(credential, runner=fake_run, delete_file=True))

            self.assertFalse(credential.exists())
            self.assertEqual(len(seen_envs), 2)
            self.assertTrue(all(env.get("CLOUDSDK_CONFIG") for env in seen_envs))
            self.assertNotEqual(seen_envs[0]["CLOUDSDK_CONFIG"], str(pathlib.Path.home() / ".config" / "gcloud"))

    def test_full_launch_composes_existing_contracts_and_marks_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential = pathlib.Path(tmp) / "bootstrap.json"
            credential.write_text("{}", encoding="utf-8")
            workspace = pathlib.Path(tmp) / "workspace"
            fake_cloud = types.SimpleNamespace(load_service_account_key=lambda path: {"type": "service_account"})
            doctors: list[list[str]] = []

            def capture_operator(args, *, runner, capture=False):
                doctors.append(args)

            with mock.patch.object(launch, "_require_command"), mock.patch.object(
                launch, "_load_script", return_value=fake_cloud
            ), mock.patch.object(launch, "_ensure_release_exists", return_value=TEST_RELEASE), mock.patch.object(
                launch, "_ensure_workspace_and_repo"
            ) as ensure_workspace, mock.patch.object(launch, "_ensure_runtime_secrets") as ensure_secrets, mock.patch.object(
                launch, "_repo_variables", side_effect=[{}, self.ready_variables()]
            ), mock.patch.object(launch, "_run_cloud_bootstrap") as cloud_bootstrap, mock.patch.object(
                launch, "_run_ids", return_value={10}
            ), mock.patch.object(launch, "_activate_dev") as activate, mock.patch.object(
                launch, "_wait_for_new_cloud_run", return_value=11
            ), mock.patch.object(launch, "_set_verified") as verified, mock.patch.object(
                launch, "_revoke_bootstrap_key", return_value=True
            ) as revoke, mock.patch.object(launch, "_run_operator", side_effect=capture_operator):
                result = launch.launch(
                    client_slug="acme",
                    github_repo="kingyx3/acme-ai-workspace",
                    project_id="acme-project-12345",
                    credential_file=credential,
                    workspace=workspace,
                    runner=mock.Mock(),
                )

            self.assertEqual(result, 0)
            ensure_workspace.assert_called_once()
            ensure_secrets.assert_called_once()
            cloud_bootstrap.assert_called_once()
            activate.assert_called_once()
            verified.assert_called_once()
            revoke.assert_called_once()
            self.assertEqual([call[-1] for call in doctors], ["dev", "prod"])
            self.assertTrue(all(call[0] == "doctor" for call in doctors))

    def test_ready_cloud_configuration_with_different_project_fails_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential = pathlib.Path(tmp) / "bootstrap.json"
            credential.write_text("{}", encoding="utf-8")
            workspace = pathlib.Path(tmp) / "workspace"
            variables = self.ready_variables()
            variables["GCP_PROJECT_ID"] = "other-project-12345"
            fake_cloud = types.SimpleNamespace(load_service_account_key=lambda path: {"type": "service_account"})

            with mock.patch.object(launch, "_require_command"), mock.patch.object(
                launch, "_load_script", return_value=fake_cloud
            ), mock.patch.object(launch, "_ensure_release_exists", return_value=TEST_RELEASE), mock.patch.object(
                launch, "_ensure_workspace_and_repo"
            ), mock.patch.object(launch, "_ensure_runtime_secrets"), mock.patch.object(
                launch, "_repo_variables", return_value=variables
            ), mock.patch.object(launch, "_run_cloud_bootstrap") as cloud_bootstrap:
                with self.assertRaisesRegex(ValueError, "deliberate migration"):
                    launch.launch(
                        client_slug="acme",
                        github_repo="kingyx3/acme-ai-workspace",
                        project_id="acme-project-12345",
                        credential_file=credential,
                        workspace=workspace,
                        runner=mock.Mock(),
                    )
            cloud_bootstrap.assert_not_called()

    def test_release_preflight_rejects_unpublished_version(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="not found"))
        with self.assertRaisesRegex(ValueError, "not published"):
            launch._ensure_release_exists(runner)


if __name__ == "__main__":
    unittest.main()
