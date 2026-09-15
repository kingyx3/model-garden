from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPERATOR = ROOT / "scripts" / "client-operator.py"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-client-workspace.py"

operator_spec = importlib.util.spec_from_file_location("client_operator", OPERATOR)
operator = importlib.util.module_from_spec(operator_spec)
assert operator_spec.loader is not None
operator_spec.loader.exec_module(operator)

bootstrap_spec = importlib.util.spec_from_file_location("bootstrap_client_workspace", BOOTSTRAP)
bootstrapper = importlib.util.module_from_spec(bootstrap_spec)
assert bootstrap_spec.loader is not None
bootstrap_spec.loader.exec_module(bootstrapper)


class ClientOperatorTests(unittest.TestCase):
    def test_doctor_accepts_role_generic_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            bootstrapper.bootstrap(workspace, "acme", "sales-assistant", "Sales Assistant")
            self.assertEqual(operator.doctor(workspace, "dev", None), 0)

    def test_configure_dry_run_derives_secret_refs_without_prompting_or_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            (workspace / "environments").mkdir(parents=True)
            (workspace / "environments" / "dev.yaml").write_text(
                """environment: dev\nruntime:\n  profile: sales-assistant\n  model_credential_ref: secret://model/dev\nconnectors:\n  google_calendar:\n    token_ref: secret://google/dev\n""",
                encoding="utf-8",
            )
            with mock.patch.object(operator, "_run") as run, mock.patch.object(operator.getpass, "getpass") as getpass:
                operator.configure(workspace, "kingyx3/acme-ai-workspace", "dev", "acme-docker", dry_run=True)
            run.assert_not_called()
            getpass.assert_not_called()

    def test_configure_writes_exact_secret_map_to_github_environment_via_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            (workspace / "environments").mkdir(parents=True)
            (workspace / "environments" / "prod.yaml").write_text(
                """environment: prod\nruntime:\n  profile: receptionist\n  model_credential_ref: secret://model/prod\nconnectors:\n  google_calendar:\n    token_ref: secret://google/prod\n""",
                encoding="utf-8",
            )
            calls = []

            def fake_run(command, *, cwd=None, input_text=None, capture=True):
                calls.append((command, input_text))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(operator, "_require_command"), mock.patch.object(operator, "_run", side_effect=fake_run), mock.patch.object(
                operator.getpass,
                "getpass",
                side_effect=["google-secret", "model-secret"],
            ):
                operator.configure(workspace, "kingyx3/acme-ai-workspace", "prod", "acme-prod-docker")

            secret_calls = [(command, stdin) for command, stdin in calls if command[:3] == ["gh", "secret", "set"]]
            self.assertEqual(len(secret_calls), 1)
            command, stdin = secret_calls[0]
            self.assertNotIn("google-secret", " ".join(command))
            self.assertNotIn("model-secret", " ".join(command))
            payload = json.loads(stdin)
            self.assertEqual(
                payload,
                {
                    "secret://google/prod": "google-secret",
                    "secret://model/prod": "model-secret",
                },
            )
            variable_calls = [command for command, _ in calls if command[:3] == ["gh", "variable", "set"]]
            self.assertEqual(len(variable_calls), 1)
            self.assertIn("acme-prod-docker", variable_calls[0])

    def test_configure_rejects_raw_credential_fields_before_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            (workspace / "environments").mkdir(parents=True)
            (workspace / "environments" / "dev.yaml").write_text(
                """environment: dev\nruntime:\n  profile: receptionist\n  api_key: should-not-be-here\n""",
                encoding="utf-8",
            )
            with mock.patch.object(operator.getpass, "getpass") as getpass:
                with self.assertRaisesRegex(ValueError, "raw credential"):
                    operator.configure(workspace, "kingyx3/acme-ai-workspace", "dev", "acme-docker")
            getpass.assert_not_called()

    def test_runtime_approval_hides_docker_paths_and_targets_governed_sidecar(self):
        request_id = "a" * 64
        calls = []

        def fake_run(command, *, cwd=None, input_text=None, capture=True):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(command, 0, stdout="container-123\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout='{"approved": true}\n', stderr="")

        with mock.patch.object(operator, "_require_command"), mock.patch.object(operator, "_run", side_effect=fake_run):
            self.assertEqual(
                operator.runtime_approval(
                    "acme-prod",
                    "approve",
                    request_id=request_id,
                    approver="owner@example.invalid",
                ),
                0,
            )

        self.assertIn("label=com.docker.compose.project=acme-prod", calls[0])
        self.assertIn("label=com.docker.compose.service=governed-tools", calls[0])
        exec_command = calls[1]
        self.assertEqual(exec_command[:3], ["docker", "exec", "container-123"])
        self.assertIn("/opt/model-garden-platform/scripts/runtime-approval.py", exec_command)
        self.assertIn("/opt/data/.modelgarden/approvals", exec_command)
        self.assertIn(request_id, exec_command)
        self.assertIn("owner@example.invalid", exec_command)

    def test_runtime_approval_requires_exact_single_governed_container(self):
        with mock.patch.object(operator, "_require_command"), mock.patch.object(
            operator,
            "_run",
            return_value=subprocess.CompletedProcess(["docker", "ps"], 0, stdout="one\ntwo\n", stderr=""),
        ):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                operator.runtime_approval("acme-dev", "list")

    def test_runtime_approval_rejects_unsafe_project_or_request(self):
        with self.assertRaisesRegex(ValueError, "project name"):
            operator.runtime_approval("../acme", "list")
        with mock.patch.object(operator, "_governed_tool_container", return_value="container"):
            with self.assertRaisesRegex(ValueError, "request id"):
                operator.runtime_approval("acme-prod", "approve", request_id="not-a-hash", approver="owner")


if __name__ == "__main__":
    unittest.main()
