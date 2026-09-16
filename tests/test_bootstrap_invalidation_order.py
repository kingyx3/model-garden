from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap-cloud.py"
spec = importlib.util.spec_from_file_location("bootstrap_cloud_order", SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bootstrap)


class BootstrapInvalidationOrderTests(unittest.TestCase):
    def test_prior_verification_is_invalidated_before_terraform_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            credential = root / "bootstrap.json"
            credential.write_text(
                json.dumps(
                    {
                        "type": "service_account",
                        "project_id": "bootstrap-admin-project",
                        "private_key_id": "key-id-123",
                        "private_key": "secret-private-key",
                        "client_email": "bootstrap@example.iam.gserviceaccount.com",
                    }
                ),
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            terraform_outputs = {
                "project_id": "acme-prod-12345",
                "region": "asia-southeast1",
                "terraform_state_bucket": "model-garden-acme-prod-12345-abcdef12-tfstate",
                "workload_identity_provider": "projects/123/locations/global/workloadIdentityPools/pool/providers/github",
                "deployer_service_account": "deploy@acme-prod-12345.iam.gserviceaccount.com",
                "runtime_service_account": "runtime@acme-prod-12345.iam.gserviceaccount.com",
                "infrastructure_owner": "client",
            }
            output_json = json.dumps({name: {"value": value} for name, value in terraform_outputs.items()})

            def fake_run(command, *, cwd=None, env=None, input_text=None):
                calls.append(list(command))
                if command[:3] == ["terraform", "output", "-json"]:
                    return subprocess.CompletedProcess(command, 0, stdout=output_json, stderr="")
                if command[:2] == ["gh", "api"] and "--method" not in command:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="not found")
                return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

            with mock.patch.object(bootstrap, "_require_command"):
                bootstrap.bootstrap_gcp(
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

            apply_index = next(i for i, command in enumerate(calls) if command[:2] == ["terraform", "apply"])
            verified_false_index = next(
                i
                for i, command in enumerate(calls)
                if command[:4] == ["gh", "variable", "set", bootstrap.VERIFIED_VARIABLE]
                and command[-1] == "false"
            )
            cloud_ready_false_index = next(
                i
                for i, command in enumerate(calls)
                if command[:4] == ["gh", "variable", "set", "MODEL_GARDEN_CLOUD_READY"]
                and command[-1] == "false"
            )
            cloud_ready_true_index = next(
                i
                for i, command in enumerate(calls)
                if command[:4] == ["gh", "variable", "set", "MODEL_GARDEN_CLOUD_READY"]
                and command[-1] == "true"
            )

            self.assertLess(verified_false_index, apply_index)
            self.assertLess(cloud_ready_false_index, apply_index)
            self.assertGreater(cloud_ready_true_index, apply_index)


if __name__ == "__main__":
    unittest.main()
