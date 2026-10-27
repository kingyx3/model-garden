"""Credential-free checks for reviewed independent verifier timer bundles (#80)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ledger_verifier_units", ROOT / "scripts" / "render-ledger-verifier-units.py"
)
UNITS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = UNITS
SPEC.loader.exec_module(UNITS)


class IndependentVerifierUnitTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            "client_project": "acme-dev-mg",
            "confirm_client_project": "acme-dev-mg",
            "gcp_project": "client-gcp-123",
            "confirm_gcp_project": "client-gcp-123",
            "bucket": "acme-ledger-locked",
            "prefix": "acme-dev-mg",
            "verifier_identity": "auditor@client-gcp-123.iam.gserviceaccount.com",
            "runtime_identity": "runtime@client-gcp-123.iam.gserviceaccount.com",
            "service_user": "mg-verifier",
        }

    def render(self, **changes):
        return UNITS.render_units(**(self.options | changes))

    def test_service_and_timer_are_tenant_bound_hardened_and_review_only(self):
        bundle = self.render()
        self.assertEqual(bundle["schemaVersion"], 1)
        self.assertEqual(bundle["clientProject"], "acme-dev-mg")
        self.assertFalse(bundle["installed"])
        self.assertFalse(bundle["notificationsDelivered"])
        self.assertEqual(set(bundle["units"]), {
            "model-garden-ledger-verifier-acme-dev-mg.service",
            "model-garden-ledger-verifier-acme-dev-mg.timer",
        })
        service = bundle["units"]["model-garden-ledger-verifier-acme-dev-mg.service"]
        timer = bundle["units"]["model-garden-ledger-verifier-acme-dev-mg.timer"]
        for expected in (
            "User=mg-verifier", "Type=oneshot", "NoNewPrivileges=yes",
            "ProtectSystem=strict", "PrivateTmp=yes", "UMask=0077",
            "ReadOnlyPaths=/srv/model-garden-verifier/acme-dev-mg/audit.jsonl",
            "ReadWritePaths=/var/lib/model-garden-verifier/acme-dev-mg",
            "--confirm-client-project acme-dev-mg",
            "--confirm-gcp-project client-gcp-123",
            "--verifier-identity auditor@client-gcp-123.iam.gserviceaccount.com",
            "--runtime-identity runtime@client-gcp-123.iam.gserviceaccount.com",
        ):
            self.assertIn(expected, service)
        self.assertIn("OnBootSec=5min", timer)
        self.assertIn("OnCalendar=*-*-* *:0/5:00", timer)
        self.assertNotIn("OnUnitInactiveSec=", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=model-garden-ledger-verifier-acme-dev-mg.service", timer)
        self.assertNotIn("systemctl enable", service + timer)
        self.assertNotIn("ExecStart=/bin/sh", service)

    def test_wrong_tenant_project_or_prefix_is_refused(self):
        for changes in (
            {"confirm_client_project": "other-dev"},
            {"confirm_gcp_project": "other-gcp"},
            {"prefix": "another-tenant"},
            {"prefix": "acme-dev-mg/../other"},
            {"client_project": "other/dev"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(UNITS.UnitError):
                    self.render(**changes)

    def test_shell_and_systemd_unit_injection_are_refused(self):
        for changes in (
            {"bucket": "valid-bucket;curl attacker"},
            {"prefix": "acme-dev-mg/%n"},
            {"service_user": "root"},
            {"service_user": "mg-verifier\nExecStart=/bin/false"},
            {"verifier_identity": "audit@client-gcp-123.iam.gserviceaccount.com\nEnvironment=SECRET"},
            {"gcp_project": "project;rm"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(UNITS.UnitError):
                    self.render(**changes)

    def test_shared_or_invalid_identity_is_refused(self):
        with self.assertRaises(UNITS.UnitError):
            self.render(verifier_identity=self.options["runtime_identity"])
        with self.assertRaises(UNITS.UnitError):
            self.render(runtime_identity="not-a-service-account")

    def test_cli_produces_only_reviewable_json_and_no_install_side_effects(self):
        argv = [
            sys.executable, str(ROOT / "scripts" / "render-ledger-verifier-units.py"),
        ]
        for key, value in self.options.items():
            argv.extend(["--" + key.replace("_", "-"), value])
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertFalse(parsed["installed"])
        self.assertIn("units", parsed)
        self.assertNotIn("secret", result.stdout.lower())

    def test_cli_refusal_is_redacted(self):
        argv = [
            sys.executable, str(ROOT / "scripts" / "render-ledger-verifier-units.py"),
        ]
        for key, value in (self.options | {"bucket": "bad-bucket\nSECRET_TOKEN"}).items():
            argv.extend(["--" + key.replace("_", "-"), value])
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("SECRET_TOKEN", result.stderr)
        self.assertNotIn("SECRET_TOKEN", result.stdout)


if __name__ == "__main__":
    unittest.main()
