"""Credential-free LiveKit-native voice readiness tests (#231).

The pinned LiveKit AgentServer provides a private HTTP health endpoint. A
running container is not proof it is connected to LiveKit or can accept calls.
No SIP, cloud or tenant credentials are used.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("voice_native_health_under_test", ROOT / "scripts" / "deploy-reference-voice.py")
VOICE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = VOICE
SPEC.loader.exec_module(VOICE)


def ps(*rows):
    return subprocess.CompletedProcess(["docker", "compose", "ps"], 0, json.dumps(list(rows)), "")


def service(name, *, health="healthy", state="running"):
    return {"Service": name, "Health": health, "State": state}


class VoiceNativeHealthTests(unittest.TestCase):
    def test_native_probe_uses_only_private_loopback_and_rejects_503_redirect_and_timeout(self):
        check = VOICE.voice_healthcheck()
        self.assertEqual(check["test"][:2], ["CMD", "python"])
        self.assertEqual(check["test"][2], "-c")
        self.assertEqual(check["interval"], "10s")
        self.assertEqual(check["timeout"], "5s")
        script = check["test"][3]
        # http.client avoids proxy environment variables and does not follow
        # redirects off loopback, unlike urllib's default URL opener.
        with mock.patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value.status = 200
            with self.assertRaises(SystemExit) as exit_status:
                exec(script, {})
            self.assertEqual(exit_status.exception.code, 0)
            connection.assert_called_once_with("127.0.0.1", 8081, timeout=3)
            connection.return_value.request.assert_called_once_with("GET", "/")
        for code in (503, 302):
            with self.subTest(status=code), mock.patch("http.client.HTTPConnection") as connection:
                connection.return_value.getresponse.return_value.status = code
                with self.assertRaises(SystemExit) as exit_status:
                    exec(script, {})
                self.assertEqual(exit_status.exception.code, 1)
        with mock.patch("http.client.HTTPConnection") as connection:
            connection.return_value.request.side_effect = TimeoutError("stalled")
            with self.assertRaises(TimeoutError):
                exec(script, {})

    def test_livekit_health_server_is_not_listening_on_compose_network(self):
        worker = (ROOT / "platform" / "channels" / "livekit_receptionist.py").read_text()
        self.assertIn('server = AgentServer(host="127.0.0.1", port=8081)', worker)

    def test_running_but_starting_voice_waits_until_native_health_is_healthy(self):
        rows = [
            ps(service("hermes"), service("voice", health="starting"), service("governed-tools")),
            ps(service("hermes"), service("voice"), service("governed-tools")),
        ]
        with mock.patch.object(VOICE, "_run", side_effect=rows) as run, \
             mock.patch.object(VOICE.time, "monotonic", side_effect=[0, 1, 2]), \
             mock.patch.object(VOICE.time, "sleep") as sleep:
            self.assertTrue(VOICE._health("client-dev", [pathlib.Path("base.yaml")], {}, timeout=10))
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_running_without_native_health_is_never_accepted(self):
        with mock.patch.object(VOICE, "_run", return_value=ps(service("hermes"), service("voice", health=""))) as run, \
             mock.patch.object(VOICE.time, "monotonic", side_effect=[0, 1, 2]), \
             mock.patch.object(VOICE.time, "sleep"):
            self.assertFalse(VOICE._health("client-dev", [], {}, timeout=2))
        self.assertEqual(run.call_count, 1)

    def test_unhealthy_native_voice_fails_without_waiting_or_claiming_success(self):
        with mock.patch.object(VOICE, "_run", return_value=ps(service("hermes"), service("voice", health="unhealthy"))), \
             mock.patch.object(VOICE.time, "monotonic", side_effect=[0, 1]), \
             mock.patch.object(VOICE.time, "sleep") as sleep:
            self.assertFalse(VOICE._health("client-dev", [], {}, timeout=10))
        sleep.assert_not_called()

    def test_compose_json_lines_and_invalid_output_fail_closed(self):
        rows = [service("hermes"), service("voice")]
        ndjson = "\n".join(json.dumps(row) for row in rows)
        with mock.patch.object(VOICE, "_run", return_value=subprocess.CompletedProcess([], 0, ndjson, "")), \
             mock.patch.object(VOICE.time, "monotonic", side_effect=[0, 1]), \
             mock.patch.object(VOICE.time, "sleep"):
            self.assertTrue(VOICE._health("client-dev", [], {}, timeout=10))
        with mock.patch.object(VOICE, "_run", return_value=subprocess.CompletedProcess([], 0, '[{"Service":"voice"}, "invalid"]', "")), \
             mock.patch.object(VOICE.time, "monotonic", side_effect=[0, 1, 2]), \
             mock.patch.object(VOICE.time, "sleep"):
            self.assertFalse(VOICE._health("client-dev", [], {}, timeout=2))

    def test_generated_voice_overlay_checks_native_health_without_exposing_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = pathlib.Path(tmp)
            (bundle / "profile").mkdir()
            (bundle / "profile" / "config.yaml").write_text("model:\n  provider: openai\n")
            (bundle / "compose.yaml").write_text(yaml.safe_dump({"services": {"hermes": {}, "governed-tools": {}}}))
            captured = []

            def record(command, **kwargs):
                overlay = pathlib.Path(command[command.index("-f", command.index("-f") + 1) + 1])
                captured.append(yaml.safe_load(overlay.read_text()))
                return subprocess.CompletedProcess(command, 0, "", "")

            binding = {"spec": {"channels": {"voice": {"url": "wss://example.test", "api_key_ref": "secret://voice/key",
                                                       "api_secret_ref": "secret://voice/secret"}}}}
            with mock.patch.object(VOICE.DEPLOY, "_validate_binding", return_value=(None, "secret://model")), \
                 mock.patch.object(VOICE.DEPLOY, "resolve_secrets", return_value={
                     "secret://model": "synthetic-model", "secret://voice/key": "synthetic-key",
                     "secret://voice/secret": "synthetic-secret",
                 }), \
                 mock.patch.object(VOICE, "transcript_environment", return_value={}), \
                 mock.patch.object(VOICE, "_context", return_value=bundle), \
                 mock.patch.object(VOICE, "_run", side_effect=record), \
                 mock.patch.object(VOICE, "_health", return_value=True):
                VOICE._apply_selected_voice(binding, binding["spec"]["channels"]["voice"], bundle, "client-dev", None, 10)
            self.assertEqual(len(captured), 1)
            voice = captured[0]["services"]["voice"]
            self.assertEqual(voice["healthcheck"], VOICE.voice_healthcheck())
            self.assertNotIn("ports", voice)
            self.assertNotIn("8081", json.dumps(voice.get("environment", {})))
            self.assertNotIn("synthetic-secret", json.dumps(voice["healthcheck"]))


if __name__ == "__main__":
    unittest.main()
