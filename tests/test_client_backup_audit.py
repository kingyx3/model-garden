"""Credential-free, adversarial tests for read-only backup slot audit (#230)."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_backup_audit", ROOT / "scripts" / "client-backup-audit.py"
)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AUDIT)
UTC = dt.timezone.utc


class BackupSlotAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.policy_dir = self.root / "policy"
        self.claims = self.root / "claims"
        self.policy_dir.mkdir(mode=0o700)
        self.claims.mkdir(mode=0o700)
        self.policy = self.policy_dir / "schedule.json"
        self.project = "acme-dev"
        self.stamp = "20261023T120000Z"
        self.recipient = "age1" + "q" * 55
        self.since = dt.datetime(2026, 10, 23, 0, tzinfo=UTC)
        self.now = dt.datetime(2026, 10, 23, 12, 6, tzinfo=UTC)
        self.write(self.policy, {
            "schemaVersion": 1, "clientProject": self.project,
            "clientSlug": "acme", "environment": "dev",
            "gcpProjectId": "sample-project-123",
            "ageRecipientSha256": hashlib.sha256(self.recipient.encode()).hexdigest(),
            "approvedBy": "operator-1", "changeRef": "CHG-123",
            "validFrom": "2026-10-20T00:00:00Z",
            "expiresAt": "2026-11-15T00:00:00Z",
            "weekdaysUTC": [4], "hourUTC": 12, "minuteUTC": 0,
        })
        self.kwargs = {
            "schedule": self.policy, "claims": self.claims,
            "project": self.project, "slug": "acme", "environment": "dev",
            "gcp_project": "sample-project-123", "recipient": self.recipient,
            "confirm_project": self.project, "since": self.since, "now": self.now,
        }

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)

    def claim(self, *, project=None):
        self.write(self.claims / f"{self.project}-{self.stamp}.claimed", {
            "clientProject": project or self.project, "slotUTC": self.stamp,
            "status": "attempted",
        })

    def receipt(self, status, *, project=None):
        self.write(self.claims / f"{self.project}-{self.stamp}.result.json", {
            "clientProject": project or self.project, "slotUTC": self.stamp,
            "status": status,
        })

    def result(self, **kwargs):
        return AUDIT.audit(**(self.kwargs | kwargs))

    def status(self, **kwargs):
        result = self.result(**kwargs)
        self.assertFalse(result["recoveryProven"])
        self.assertFalse(result["notificationsDelivered"])
        self.assertEqual(result["event"], "backup.schedule.audit")
        self.assertEqual(len(result["slots"]), 1)
        self.assertEqual(result["slots"][0]["slotUTC"], self.stamp)
        return result["slots"][0]["status"]

    def test_missed_due_slot_is_not_success(self):
        self.assertEqual(self.status(), "missed")

    def test_claim_without_receipt_is_unknown_not_retried(self):
        self.claim()
        self.assertEqual(self.status(), "unknown")

    def test_uploaded_receipt_is_not_restore_proof(self):
        self.claim()
        self.receipt("uploaded-not-recovery-proven")
        self.assertEqual(self.status(), "uploaded-not-recovery-proven")

    def test_failed_receipt_is_reported_without_secret_details(self):
        self.claim()
        self.receipt("failed-not-recovery-proven")
        self.assertEqual(self.status(), "failed-not-recovery-proven")
        self.assertNotIn("age1", json.dumps(self.result()))

    def test_cross_tenant_or_invalid_receipt_is_unknown(self):
        self.claim()
        self.receipt("uploaded-not-recovery-proven", project="other-dev")
        self.assertEqual(self.status(), "unknown")
        self.receipt("recovery-proven")
        self.assertEqual(self.status(), "unknown")
        self.receipt("uploaded-not-recovery-proven")
        self.claim(project="other-dev")
        self.assertEqual(self.status(), "unknown")

    def test_symlink_world_readable_and_non_json_receipts_are_unknown(self):
        self.claim()
        receipt = self.claims / f"{self.project}-{self.stamp}.result.json"
        target = self.root / "unrelated"
        target.write_text("do-not-read", encoding="utf-8")
        receipt.symlink_to(target)
        self.assertEqual(self.status(), "unknown")
        receipt.unlink()
        self.receipt("uploaded-not-recovery-proven")
        receipt.chmod(0o644)
        self.assertEqual(self.status(), "unknown")
        receipt.chmod(0o600)
        receipt.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.status(), "unknown")

    def test_orphan_receipt_and_insecure_claim_are_unknown(self):
        self.receipt("uploaded-not-recovery-proven")
        self.assertEqual(self.status(), "unknown")
        self.claim()
        (self.claims / f"{self.project}-{self.stamp}.claimed").chmod(0o644)
        self.assertEqual(self.status(), "unknown")

    def test_grace_period_and_non_scheduled_days_are_not_missed(self):
        early = self.now.replace(minute=4)
        self.assertEqual(self.result(now=early)["slots"], [])
        saturday = dt.datetime(2026, 10, 24, 13, tzinfo=UTC)
        self.assertEqual(self.result(since=saturday, now=saturday)["slots"], [])

    def test_policy_binding_private_directory_and_bounded_since(self):
        for changed in ({"project": "other-dev"}, {"environment": "prod"},
                        {"recipient": "age1" + "p" * 55},
                        {"confirm_project": "other-dev"},
                        {"since": dt.datetime(2026, 9, 1, tzinfo=UTC)},
                        {"since": dt.datetime(2026, 10, 24, tzinfo=UTC)}):
            with self.subTest(changed=changed), self.assertRaises(
                    (AUDIT.SCHEDULE.ScheduleError, ValueError)):
                self.result(**changed)
        self.claims.chmod(0o755)
        with self.assertRaises(AUDIT.SCHEDULE.ScheduleError):
            self.result()

    def test_cli_failure_redacts_inputs(self):
        with mock.patch.object(AUDIT, "audit", side_effect=ValueError("secret://must-not-log")), \
             mock.patch("sys.stderr") as stderr:
            args = []
            for key in ("schedule", "claims", "project", "client-slug", "environment",
                        "gcp-project-id", "recipient", "confirm-project", "since-utc"):
                args.extend(["--" + key, "placeholder"])
            self.assertEqual(AUDIT.main(args), 2)
            self.assertNotIn("must-not-log", str(stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
