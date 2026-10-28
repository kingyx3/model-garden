"""Credential-free, fail-closed reviewed systemd backup timer checks (#230)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-client-backup-units.py"
SPEC = importlib.util.spec_from_file_location("client_backup_units", SCRIPT)
UNITS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = UNITS
SPEC.loader.exec_module(UNITS)


class ClientBackupUnitTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            "client_slug": "acme", "environment": "dev", "client_project": "acme-dev",
            "confirm_client_project": "acme-dev", "gcp_project": "acme-gcp-123",
            "confirm_gcp_project": "acme-gcp-123", "service_user": "mg-backup",
            "age_recipient": "age1" + "a" * 58,
            "snapshot_image": "docker.io/library/busybox@sha256:" + "a" * 64,
        }

    def render(self, **changes):
        return UNITS.render_units(**(self.options | changes))

    def test_review_only_timer_and_service_delegate_to_preapproved_dispatch(self):
        bundle = self.render()
        self.assertEqual(bundle["clientProject"], "acme-dev")
        self.assertEqual(bundle["schemaVersion"], 1)
        self.assertFalse(bundle["installed"])
        self.assertFalse(bundle["backupProven"])
        self.assertFalse(bundle["notificationsDelivered"])
        self.assertEqual(set(bundle["units"]), {
            "model-garden-client-backup-acme-dev.service",
            "model-garden-client-backup-acme-dev.timer",
        })
        service = bundle["units"]["model-garden-client-backup-acme-dev.service"]
        timer = bundle["units"]["model-garden-client-backup-acme-dev.timer"]
        for item in (
            "Type=oneshot", "User=mg-backup", "Group=mg-backup", "NoNewPrivileges=yes",
            "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "UMask=0077",
            "KillMode=control-group", "TimeoutStartSec=3700s",
            "ReadWritePaths=/var/lib/model-garden-backup/acme-dev /run/lock/model-garden",
            "ReadOnlyPaths=/srv/model-garden/acme-dev/compose.yaml",
            "--schedule /var/lib/model-garden-backup/acme-dev/policy/schedule.json",
            "--approvals /var/lib/model-garden-backup/acme-dev/approvals",
            "--claims /var/lib/model-garden-backup/acme-dev/claims",
            "--compose /srv/model-garden/acme-dev/compose.yaml",
            "--project acme-dev", "--confirm-project acme-dev",
            "--client-slug acme", "--environment dev",
            "--gcp-project-id acme-gcp-123",
            "--recipient age1" + "a" * 58,
            "--image docker.io/library/busybox@sha256:" + "a" * 64,
        ):
            self.assertIn(item, service)
        self.assertIn("OnCalendar=*-*-* *:*:00", timer)
        self.assertIn("AccuracySec=1s", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=model-garden-client-backup-acme-dev.service", timer)
        self.assertNotIn("ExecStart=/bin/sh", service)
        self.assertNotIn("EnvironmentFile=", service)
        self.assertNotIn("systemctl enable", service + timer)
        self.assertNotIn("ExecStartPre=", service)
        self.assertNotIn("ExecStartPost=", service)
        self.assertNotIn("docker compose up", service)

    def test_tenant_environment_and_explicit_confirmations_fail_closed(self):
        for change in (
            {"client_project": "other-dev"}, {"confirm_client_project": "other-dev"},
            {"environment": "prod"}, {"environment": "dev\nExecStart=/bin/false"},
            {"confirm_gcp_project": "other-gcp"}, {"gcp_project": "invalid"},
            {"client_slug": "other"}, {"client_slug": "../../other"},
        ):
            with self.subTest(change=change), self.assertRaises(UNITS.UnitError):
                self.render(**change)

    def test_systemd_shell_and_service_identity_injection_fail_closed(self):
        for change in (
            {"service_user": "root"}, {"service_user": "nobody"},
            {"service_user": "mg-backup\nEnvironment=SECRET"},
            {"age_recipient": "age1" + "a" * 58 + "\nExecStart=/bin/false"},
            {"snapshot_image": "docker.io/library/busybox:latest"},
            {"snapshot_image": "docker.io/library/busybox@sha256:" + "b" * 64 + ";true"},
            {"gcp_project": "acme-gcp-123%n"},
        ):
            with self.subTest(change=change), self.assertRaises(UNITS.UnitError):
                self.render(**change)

    def test_other_tenant_renders_separate_paths_and_units(self):
        other = self.render(client_slug="beta", client_project="beta-dev",
                            confirm_client_project="beta-dev")
        combined = json.dumps(other)
        self.assertNotIn("acme-dev", combined)
        self.assertIn("/var/lib/model-garden-backup/beta-dev", combined)
        self.assertIn("/srv/model-garden/beta-dev/compose.yaml", combined)

    def test_cli_is_review_only_and_returns_redacted_refusals(self):
        argv = [sys.executable, str(SCRIPT)]
        for key, value in self.options.items():
            argv.extend(["--" + key.replace("_", "-"), value])
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["installed"])
        bad = argv.copy()
        bad[bad.index("--age-recipient") + 1] += "\nSECRET_TOKEN"
        result = subprocess.run(bad, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("SECRET_TOKEN", result.stderr + result.stdout)
        self.assertIn("unsafe-or-unconfirmed-configuration", result.stderr)


if __name__ == "__main__":
    unittest.main()
