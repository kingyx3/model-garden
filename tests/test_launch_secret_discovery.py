from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "launch-client.py"
spec = importlib.util.spec_from_file_location("launch_client_secret_discovery", SCRIPT)
launch_client = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(launch_client)


class RuntimeSecretDiscoveryTests(unittest.TestCase):
    def test_detects_exact_environment_secret_from_json(self):
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='[{"name":"MODEL_GARDEN_RUNTIME_SECRETS_JSON"},{"name":"OTHER"}]\n',
                stderr="",
            )

        self.assertTrue(launch_client._runtime_secret_present("kingyx3/acme-ai-workspace", "dev", runner))
        self.assertEqual(commands[0][-2:], ["--json", "name"])

    def test_missing_environment_secret_is_reported_as_absent(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='[{"name":"OTHER"}]\n', stderr="")

        self.assertFalse(launch_client._runtime_secret_present("kingyx3/acme-ai-workspace", "prod", runner))

    def test_secret_read_failure_stops_launch_instead_of_reconfiguring(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="permission denied")

        with self.assertRaisesRegex(ValueError, "read GitHub dev environment secrets failed"):
            launch_client._runtime_secret_present("kingyx3/acme-ai-workspace", "dev", runner)

    def test_malformed_secret_response_fails_closed(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='{"name":"not-a-list"}\n', stderr="")

        with self.assertRaisesRegex(ValueError, "unexpected response shape"):
            launch_client._runtime_secret_present("kingyx3/acme-ai-workspace", "dev", runner)


if __name__ == "__main__":
    unittest.main()
