from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap-cloud.py"
spec = importlib.util.spec_from_file_location("bootstrap_cloud", SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bootstrap)


class BootstrapCloudTests(unittest.TestCase):
    def _credential(self, root: pathlib.Path) -> pathlib.Path:
        path = root / "bootstrap.json"
        path.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "bootstrap-admin-project",
                    "private_key_id": "key-id-123",
                    "private_key": "SUPER-SECRET-BOOTSTRAP-PRIVATE-KEY",
                    "client_email": "bootstrap@example.iam.gserviceaccount.com",
                }
            ),
            encoding="utf-8",
        )
        return path

    def _terraform_output(self) -> str:
        values = {
            "project_id": "acme-prod-12345",
            "region": "asia-southeast1",
            "terraform_state_bucket": "model-garden-acme-prod-12345-abcdef12-tfstate",
            "workload_identity_provider": "projects/123/locations/global/workloadIdentityPools/mg-github-abcdef12/providers/github",
            "deployer_service_account": "mg-deploy-abcdef12@acme-prod-12345.iam.gserviceaccount.com",
            "runtime_service_account": "mg-runtime-abcdef12@acme-prod-12345.iam.gserviceaccount.com",
            "infrastructure_owner": "client",
        }
        return json.dumps({key: {"value": value} for key, value in values.items()})

    def test_dry_run_validates_credential_but_never_returns_private_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            result = bootstrap.bootstrap_gcp(
                project_id="acme-prod-12345",
                github_repo="kingyx3/acme-ai-workspace",
                client_slug="acme",
                agent_slug="sales-assistant",
                region="asia-southeast1",
                zone="asia-southeast1-b",
                infrastructure_owner="client",
                credential_path=self._credential(root),
                state_base=root / "state",
                dry_run=True,
            )

            encoded = json.dumps(result)
            self.assertNotIn("SUPER-SECRET", encoded)
            self.assertNotIn("private_key", encoded)
            self.assertEqual(result["agent_slug"], "sales-assistant")
            self.assertEqual(result["infrastructure_owner"], "client")

    def test_rendered_cloud_workflow_is_keyless_and_role_generic(self):
        workflow = bootstrap.render_cloud_workflow("acme", "operations-coordinator")

        self.assertIn("google-github-actions/auth@v3", workflow)
        self.assertIn("workload_identity_provider: ${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}", workflow)
        self.assertIn("service_account: ${{ vars.GCP_DEPLOY_SERVICE_ACCOUNT }}", workflow)
        self.assertIn("--agent operations-coordinator", workflow)
        self.assertIn("client_slug=acme", workflow)
        self.assertIn("MODEL_GARDEN_RUNTIME_SECRETS_JSON", workflow)
        self.assertNotIn("credentials_json", workflow)
        self.assertNotIn("GOOGLE_CREDENTIALS", workflow)
        self.assertNotIn("GCP_CREDENTIALS", workflow)
        self.assertNotIn("__CLIENT_SLUG__", workflow)
        self.assertNotIn("__AGENT_SLUG__", workflow)

    def test_full_bootstrap_sets_only_nonsecret_github_variables_and_installs_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            credential = self._credential(root)
            calls: list[tuple[list[str], str | None]] = []

            def fake_run(command, *, cwd=None, env=None, input_text=None):
                calls.append((list(command), input_text))
                if command[:3] == ["terraform", "output", "-json"]:
                    return subprocess.CompletedProcess(command, 0, stdout=self._terraform_output(), stderr="")
                if command[:2] == ["gh", "api"] and "--method" not in command:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="not found")
                return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

            with mock.patch.object(bootstrap, "_require_command"):
                result = bootstrap.bootstrap_gcp(
                    project_id="acme-prod-12345",
                    github_repo="kingyx3/acme-ai-workspace",
                    client_slug="acme",
                    agent_slug="receptionist",
                    region="asia-southeast1",
                    zone="asia-southeast1-b",
                    infrastructure_owner="client",
                    credential_path=credential,
                    state_base=root / "state",
                    runner=fake_run,
                )

            command_text = "\n".join(" ".join(command) for command, _ in calls)
            stdin_text = "\n".join(value or "" for _, value in calls)
            self.assertNotIn("SUPER-SECRET-BOOTSTRAP-PRIVATE-KEY", command_text)
            self.assertNotIn("SUPER-SECRET-BOOTSTRAP-PRIVATE-KEY", stdin_text)
            self.assertNotIn(str(credential), command_text)

            variable_calls = [command for command, _ in calls if command[:3] == ["gh", "variable", "set"]]
            self.assertTrue(any(bootstrap.VERIFIED_VARIABLE in command and "false" in command for command in variable_calls))
            self.assertTrue(any("MODEL_GARDEN_CLOUD_READY" in command and "false" in command for command in variable_calls))
            self.assertTrue(any("MODEL_GARDEN_CLOUD_READY" in command and "true" in command for command in variable_calls))
            self.assertTrue(any("GCP_WORKLOAD_IDENTITY_PROVIDER" in command for command in variable_calls))
            self.assertFalse(any("CREDENTIAL" in " ".join(command) for command in variable_calls))

            put_calls = [(command, stdin) for command, stdin in calls if command[:3] == ["gh", "api", "--method"]]
            self.assertEqual(len(put_calls), 1)
            payload = json.loads(put_calls[0][1])
            workflow = base64.b64decode(payload["content"]).decode("utf-8")
            self.assertEqual(payload["message"], bootstrap.CLOUD_WORKFLOW_COMMIT)
            self.assertIn("workload_identity_provider", workflow)
            self.assertNotIn("SUPER-SECRET", workflow)
            self.assertEqual(result["workflow"], bootstrap.CLOUD_WORKFLOW_PATH)

    def test_invalid_credential_and_unsafe_slugs_fail_before_external_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bad = root / "bad.json"
            bad.write_text('{"type":"authorized_user"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "service-account JSON"):
                bootstrap.load_service_account_key(bad)

            credential = self._credential(root)
            with self.assertRaisesRegex(ValueError, "client slug"):
                bootstrap.bootstrap_gcp(
                    project_id="acme-prod-12345",
                    github_repo="kingyx3/acme-ai-workspace",
                    client_slug="../acme",
                    agent_slug="receptionist",
                    region="asia-southeast1",
                    zone="asia-southeast1-b",
                    infrastructure_owner="client",
                    credential_path=credential,
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()
