from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-client-docker.py"
spec = importlib.util.spec_from_file_location("deploy_client_docker_apply", SCRIPT)
deployer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(deployer)


class DeployClientDockerApplyTests(unittest.TestCase):
    def test_aggregate_secret_map_is_not_forwarded_to_docker(self):
        binding = {
            "apiVersion": "modelgarden.ai/v1",
            "kind": "EnvironmentBinding",
            "spec": {
                "environment": "dev",
                "runtime": {
                    "profile": "receptionist",
                    "model_credential_ref": "secret://model/dev",
                },
            },
        }
        calls: list[tuple[list[str], dict[str, str], bool]] = []

        def fake_run(command, *, env, check=True):
            calls.append((command, env.copy(), check))
            class Result:
                returncode = 1 if command[:3] == ["docker", "image", "inspect"] else 0
                stdout = ""
                stderr = (
                    "Error response from daemon: No such image: model-garden-acme-dev:current"
                    if command[:3] == ["docker", "image", "inspect"]
                    else ""
                )
            return Result()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(deployer, "_run", side_effect=fake_run), mock.patch.object(deployer, "_wait_healthy", return_value=True):
            with mock.patch.dict(os.environ, {"MODEL_GARDEN_RUNTIME_SECRETS_JSON": "do-not-forward"}, clear=False):
                deployer.apply_bundle(
                    pathlib.Path(tmp),
                    binding,
                    "acme-dev",
                    secrets_json=json.dumps({"secret://model/dev": "model-secret"}),
                    timeout=1,
                )

        self.assertTrue(calls)
        for _, env, _ in calls:
            self.assertNotIn("MODEL_GARDEN_RUNTIME_SECRETS_JSON", env)
            self.assertEqual(env.get("MODEL_GARDEN_MODEL_CREDENTIAL"), "model-secret")

    def test_failed_candidate_restores_previous_image(self):
        binding = {
            "apiVersion": "modelgarden.ai/v1",
            "kind": "EnvironmentBinding",
            "spec": {
                "environment": "prod",
                "runtime": {
                    "profile": "receptionist",
                    "model_credential_ref": "secret://model/prod",
                },
            },
        }
        commands: list[list[str]] = []

        def fake_run(command, *, env, check=True):
            commands.append(command)
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        health = iter([False, True])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(deployer, "_run", side_effect=fake_run), mock.patch.object(deployer, "_wait_healthy", side_effect=lambda *args, **kwargs: next(health)):
            with self.assertRaisesRegex(ValueError, "previous runtime restored"):
                deployer.apply_bundle(
                    pathlib.Path(tmp),
                    binding,
                    "acme-prod",
                    secrets_json=json.dumps({"secret://model/prod": "model-secret"}),
                    timeout=1,
                )

        self.assertIn(["docker", "tag", "model-garden-acme-prod:current", "model-garden-acme-prod:rollback"], commands)
        self.assertIn(["docker", "tag", "model-garden-acme-prod:rollback", "model-garden-acme-prod:current"], commands)
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_image_inspect_operational_failure_aborts_before_candidate_deployment(self):
        binding = {
            "apiVersion": "modelgarden.ai/v1",
            "kind": "EnvironmentBinding",
            "spec": {
                "environment": "prod",
                "runtime": {
                    "profile": "receptionist",
                    "model_credential_ref": "secret://model/prod",
                },
            },
        }
        commands: list[list[str]] = []

        def fake_run(command, *, env, check=True):
            commands.append(command)
            class Result:
                returncode = 1 if command[:3] == ["docker", "image", "inspect"] else 0
                stdout = ""
                stderr = (
                    "Cannot connect to the Docker daemon"
                    if command[:3] == ["docker", "image", "inspect"]
                    else ""
                )
            return Result()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(deployer, "_run", side_effect=fake_run):
            with self.assertRaisesRegex(ValueError, "cannot inspect current runtime image"):
                deployer.apply_bundle(
                    pathlib.Path(tmp),
                    binding,
                    "acme-prod",
                    secrets_json=json.dumps({"secret://model/prod": "model-secret"}),
                    timeout=1,
                )

        self.assertFalse(any("build" in command for command in commands))
        self.assertFalse(any("up" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
