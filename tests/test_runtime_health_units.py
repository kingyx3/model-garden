"""Review-only private runtime health sampler units (#231)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "private_runtime_units", ROOT / "scripts" / "render-runtime-health-units.py"
)
UNITS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = UNITS
SPEC.loader.exec_module(UNITS)


class RuntimeHealthUnitTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            "client": "acme",
            "environment": "dev",
            "compose_project": "acme-dev",
            "confirm_compose_project": "acme-dev",
            "project_id": "sample-project-123",
            "confirm_project_id": "sample-project-123",
            "service_user": "mg-monitor",
        }

    def render(self, **changes):
        return UNITS.render_units(**(self.options | changes))

    def test_review_only_tenant_bound_private_service_and_timer(self):
        result = self.render()
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["clientProject"], "acme-dev")
        self.assertFalse(result["installed"])
        self.assertFalse(result["monitoringProven"])
        self.assertFalse(result["notificationsDelivered"])
        name = "model-garden-runtime-health-acme-dev"
        self.assertEqual(set(result["units"]), {name + ".service", name + ".timer"})
        service = result["units"][name + ".service"]
        timer = result["units"][name + ".timer"]
        for expected in (
            "Type=oneshot", "User=mg-monitor", "SupplementaryGroups=docker",
            "NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateTmp=yes",
            "UMask=0077", "CapabilityBoundingSet=",
            "ExecStart=/usr/bin/python3 /opt/model-garden/scripts/runtime-health-metric.py",
            "--project-id sample-project-123 --client acme --environment dev --compose-project acme-dev",
        ):
            self.assertIn(expected, service)
        self.assertIn("OnBootSec=60s", timer)
        self.assertIn("OnUnitInactiveSec=60s", timer)
        self.assertIn("AccuracySec=10s", timer)
        self.assertIn("Unit=" + name + ".service", timer)
        self.assertNotIn("ExecStart=/bin/sh", service)
        self.assertNotIn("systemctl ", service + timer)

    def test_wrong_tenant_project_or_cross_environment_refused(self):
        for changes in (
            {"confirm_project_id": "another-project-123"},
            {"confirm_compose_project": "another-dev"},
            {"compose_project": "other-dev", "confirm_compose_project": "other-dev"},
            {"compose_project": "acme-prod", "confirm_compose_project": "acme-prod"},
            {"environment": "uat"},
            {"client": "another"},
        ):
            with self.subTest(changes=changes), self.assertRaises(UNITS.UnitError):
                self.render(**changes)

    def test_shell_systemd_and_path_injection_refused(self):
        for changes in (
            {"client": "acme\nExecStart=/bin/false"},
            {"client": "acme%"},
            {"project_id": "sample-project-123;curl attacker"},
            {"service_user": "mg-monitor\nEnvironment=TOKEN"},
            {"service_user": "root"},
            {"service_user": "nobody"},
            {"compose_project": "acme-dev/../../other"},
            {"compose_project": "acme-dev%n"},
            {"project_id": "sample-project-123\nSECRET"},
        ):
            with self.subTest(changes=changes), self.assertRaises(UNITS.UnitError):
                self.render(**changes)

    def test_deterministic_output_contains_no_credentials_or_external_urls(self):
        result = self.render()
        self.assertEqual(result, self.render())
        data = json.dumps(result)
        self.assertNotIn("token", data.lower())
        self.assertNotIn("https://monitoring.googleapis.com", data)
        self.assertNotIn("EnvironmentFile=", data)
        self.assertNotIn("curl ", data)

    def test_cli_emits_reviewable_json_without_installing(self):
        args = [sys.executable, str(ROOT / "scripts" / "render-runtime-health-units.py")]
        for key, value in self.options.items():
            args += ["--" + key.replace("_", "-"), value]
        completed = subprocess.run(args, capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertFalse(result["installed"])
        self.assertEqual(len(result["units"]), 2)

    def test_cli_failure_redacts_injected_value(self):
        args = [sys.executable, str(ROOT / "scripts" / "render-runtime-health-units.py")]
        for key, value in (self.options | {"service_user": "bad\nSECRET_TOKEN"}).items():
            args += ["--" + key.replace("_", "-"), value]
        completed = subprocess.run(args, capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("SECRET_TOKEN", completed.stderr + completed.stdout)


    def test_optional_voice_is_review_only_and_uses_native_private_sampler(self):
        result = self.render(include_voice=True)
        self.assertFalse(result["installed"])
        self.assertFalse(result["monitoringProven"])
        self.assertFalse(result["notificationsDelivered"])
        runtime = "model-garden-runtime-health-acme-dev"
        voice = "model-garden-voice-health-acme-dev"
        self.assertEqual(set(result["units"]), {
            runtime + ".service", runtime + ".timer",
            voice + ".service", voice + ".timer",
        })
        service = result["units"][voice + ".service"]
        timer = result["units"][voice + ".timer"]
        self.assertIn("ExecStart=/usr/bin/python3 /opt/model-garden/scripts/voice-health-metric.py", service)
        self.assertIn("--project-id sample-project-123 --client acme --environment dev --compose-project acme-dev", service)
        for required in ("User=mg-monitor", "SupplementaryGroups=docker",
                         "NoNewPrivileges=yes", "ProtectSystem=strict", "UMask=0077"):
            self.assertIn(required, service)
        self.assertIn("Unit=" + voice + ".service", timer)
        self.assertIn("OnUnitInactiveSec=60s", timer)
        self.assertNotIn("/bin/sh", service)
        self.assertNotIn("EnvironmentFile=", service)
        self.assertNotIn("token", json.dumps(result).lower())

    def test_voice_opt_in_refuses_non_boolean_and_cross_tenant_targets(self):
        for flag in ("true", "false", 1, None, ["voice"]):
            with self.subTest(flag=flag), self.assertRaises(UNITS.UnitError):
                self.render(include_voice=flag)
        with self.assertRaises(UNITS.UnitError):
            self.render(include_voice=True, confirm_project_id="other-project-123")
        self.assertEqual(len(self.render(include_voice=False)["units"]), 2)

    def test_cli_voice_opt_in_adds_only_two_reviewable_units(self):
        args = [sys.executable, str(ROOT / "scripts" / "render-runtime-health-units.py")]
        for key, value in self.options.items():
            args += ["--" + key.replace("_", "-"), value]
        completed = subprocess.run(args + ["--include-voice"], capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(len(result["units"]), 4)
        self.assertFalse(result["installed"])
        self.assertFalse(result["notificationsDelivered"])
        self.assertNotIn("voice-health-metric.py", json.dumps(self.render()))


if __name__ == "__main__":
    unittest.main()
