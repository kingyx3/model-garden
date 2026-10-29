from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
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
                """environment: dev\nruntime:\n  profile: sales-assistant\n  model_credential_ref: secret://model/dev\nconnectors:\n  crm:\n    token_ref: secret://google/dev\n""",
                encoding="utf-8",
            )
            with mock.patch.object(operator, "_run") as run, mock.patch.object(operator.getpass, "getpass") as getpass:
                operator.configure(workspace, "kingyx3/acme-ai-workspace", "dev", dry_run=True)
            run.assert_not_called()
            getpass.assert_not_called()

    def test_configure_writes_exact_secret_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            (workspace / "environments").mkdir(parents=True)
            (workspace / "environments" / "prod.yaml").write_text(
                """environment: prod\nruntime:\n  profile: receptionist\n  model_credential_ref: secret://model/prod\nconnectors:\n  crm:\n    token_ref: secret://google/prod\n""",
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
                operator.configure(workspace, "kingyx3/acme-ai-workspace", "prod")

            secret_calls = [(command, stdin) for command, stdin in calls if command[:3] == ["gh", "secret", "set"]]
            self.assertEqual(len(secret_calls), 1)
            command, stdin = secret_calls[0]
            self.assertNotIn("google-secret", " ".join(command))
            self.assertNotIn("model-secret", " ".join(command))
            self.assertEqual(
                json.loads(stdin),
                {
                    "secret://google/prod": "google-secret",
                    "secret://model/prod": "model-secret",
                },
            )
            self.assertFalse(any(command[:3] == ["gh", "variable", "set"] for command, _ in calls))

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
                    operator.configure(workspace, "kingyx3/acme-ai-workspace", "dev")
            getpass.assert_not_called()

    def test_doctor_accepts_keyless_gcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            bootstrapper.bootstrap(workspace, "acme")
            repo_vars = {name: "configured" for name in operator.KEYLESS_GCP_VARIABLES}
            repo_vars["MODEL_GARDEN_CLOUD_PROVIDER"] = "gcp"
            repo_vars["MODEL_GARDEN_CLOUD_READY"] = "true"

            def completed(command, code=0, stdout=""):
                return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")

            original_run = operator._run

            def fake_run(command, *, cwd=None, input_text=None, capture=True):
                if command[:3] == ["gh", "secret", "list"]:
                    return completed(command, stdout="MODEL_GARDEN_RUNTIME_SECRETS_JSON\tupdated\n")
                if command[:3] == ["gh", "variable", "list"]:
                    if "--env" in command:
                        return completed(command, stdout="[]")
                    return completed(
                        command,
                        stdout=json.dumps([{"name": key, "value": value} for key, value in repo_vars.items()]),
                    )
                return original_run(command, cwd=cwd, input_text=input_text, capture=capture)

            with mock.patch.object(operator, "_require_command"), mock.patch.object(operator, "_run", side_effect=fake_run):
                self.assertEqual(operator.doctor(workspace, "dev", "kingyx3/acme-ai-workspace"), 0)

    def test_keyless_gcp_readiness_fails_closed_when_provider_config_is_incomplete(self):
        ready, detail = operator._keyless_gcp_ready(
            {
                "MODEL_GARDEN_CLOUD_PROVIDER": "gcp",
                "MODEL_GARDEN_CLOUD_READY": "true",
                "GCP_PROJECT_ID": "acme",
            }
        )
        self.assertFalse(ready)
        self.assertIn("missing", detail)
        self.assertIn("GCP_WORKLOAD_IDENTITY_PROVIDER", detail)
        self.assertIn("MODEL_GARDEN_INFRA_OWNERSHIP", detail)

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


    def _connect_preview(self, *arguments):
        """Exercise the actual operator CLI without a provider, secret store or workspace mutation."""
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["client-operator.py", "connect", *arguments]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = operator.main()
        return status, output.getvalue(), errors.getvalue()

    def test_connect_requires_a_named_coworker_before_previewing_permissions(self):
        status, output, errors = self._connect_preview(
            "crm", "--provider", "built-in-crm", "--tools", "crm.read",
            "--owner", "owner@example.invalid", "--operator", "operator@example.invalid",
        )
        self.assertNotEqual(status, 0)
        self.assertEqual(output, "")
        self.assertIn("coworker", errors.lower())

    def test_connect_preview_is_agent_bound_non_authorizing_and_tier_zero_credential_free(self):
        args = ("crm", "--provider", "built-in-crm", "--tools", "crm.read",
                "--owner", "owner@example.invalid", "--operator", "operator@example.invalid")
        first, output, errors = self._connect_preview(*args, "--agent", "receptionist")
        self.assertEqual((first, errors), (0, ""))
        preview = json.loads(output)
        self.assertEqual(preview["agent"], "receptionist")
        self.assertEqual(preview["tools"], ["crm.read"])
        self.assertEqual(preview["scopes"], [])
        self.assertEqual(preview["tier"], 0)
        self.assertFalse(preview["connectable"])
        self.assertTrue(preview["previewOnly"])
        self.assertFalse(preview["ownerConsentVerified"])
        self.assertFalse(preview["providerConnectionTested"])
        self.assertFalse(preview["bindingChanged"])
        self.assertEqual(len(preview["digest"]), 64)
        other, second_output, _ = self._connect_preview(*args, "--agent", "researcher")
        self.assertEqual(other, 0)
        self.assertNotEqual(preview["digest"], json.loads(second_output)["digest"])
        self.assertNotIn("secret://", output)

    def test_planned_provider_never_offers_connection_or_scopes_even_with_a_coworker(self):
        status, output, _ = self._connect_preview(
            "calendar", "--provider", "google-calendar", "--tools", "calendar.book",
            "--agent", "receptionist", "--owner", "owner@example.invalid",
            "--operator", "operator@example.invalid",
        )
        self.assertNotEqual(status, 0)
        preview = json.loads(output)
        self.assertEqual(preview["agent"], "receptionist")
        self.assertFalse(preview["connectable"])
        self.assertEqual(preview["scopes"], [])
        self.assertIn("not implemented", preview["reason"])

    def test_mailbox_preview_requires_explicit_kind_address_and_personal_exception(self):
        base = ("mail", "--provider", "gmail", "--tools", "mail.draft",
                "--agent", "receptionist", "--owner", "owner@example.invalid",
                "--operator", "operator@example.invalid")
        status, _, error = self._connect_preview(*base)
        self.assertNotEqual(status, 0)
        self.assertIn("mailbox", error)
        status, _, error = self._connect_preview(
            *base, "--mailbox-kind", "personal", "--mailbox-address", "person@example.invalid",
        )
        self.assertNotEqual(status, 0)
        self.assertIn("personal mailbox", error)
        status, output, error = self._connect_preview(
            *base, "--mailbox-kind", "shared", "--mailbox-address", "support@example.invalid",
        )
        self.assertNotEqual(status, 0)  # Gmail remains planned; no credentials requested.
        self.assertEqual(error, "")
        preview = json.loads(output)
        self.assertEqual(preview["mailbox"], {"kind": "shared", "address": "support@example.invalid"})
        self.assertFalse(preview["connectable"])

    def test_connect_rejects_unreviewed_tool_and_unsafe_agent_identity(self):
        base = ("calendar", "--provider", "google-calendar", "--tools", "calendar.book",
                "--owner", "owner@example.invalid", "--operator", "operator@example.invalid")
        for extra in (("--agent", "../other"), ("--agent", "receptionist", "--tools", "mail.send")):
            with self.subTest(extra=extra):
                status, output, errors = self._connect_preview(*base, *extra)
                self.assertNotEqual(status, 0)
                self.assertEqual(output, "")
                self.assertTrue(errors)

    def test_runtime_approval_rejects_unsafe_project_or_request(self):
        with self.assertRaisesRegex(ValueError, "project name"):
            operator.runtime_approval("../acme", "list")
        with mock.patch.object(operator, "_governed_tool_container", return_value="container"):
            with self.assertRaisesRegex(ValueError, "request id"):
                operator.runtime_approval("acme-prod", "approve", request_id="not-a-hash", approver="owner")


if __name__ == "__main__":
    unittest.main()
