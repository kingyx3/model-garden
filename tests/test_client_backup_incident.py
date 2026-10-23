"""Adversarial, credential-free backup incident transition tests (#230)."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_backup_incident", ROOT / "scripts" / "client-backup-incident.py"
)
INCIDENT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INCIDENT)
UTC = dt.timezone.utc


class BackupIncidentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        for name in ("policy", "claims", "state"):
            (self.root / name).mkdir(mode=0o700)
        self.policy = self.root / "policy" / "schedule.json"
        self.recipient = "age1" + "q" * 55
        self.start = dt.datetime(2026, 10, 23, 0, tzinfo=UTC)
        self.now = dt.datetime(2026, 10, 23, 12, 6, tzinfo=UTC)
        self.project = "acme-dev"
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
        self.kwargs = dict(schedule=self.policy, claims=self.root / "claims",
                           state_dir=self.root / "state", project=self.project,
                           slug="acme", environment="dev", gcp_project="sample-project-123",
                           recipient=self.recipient, confirm_project=self.project,
                           since=self.start, now=self.now)
        self.stamp = "20261023T120000Z"

    @staticmethod
    def write(path, doc):
        path.write_text(json.dumps(doc), encoding="utf-8")
        path.chmod(0o600)

    def receipt(self, stamp, status="uploaded-not-recovery-proven"):
        self.write(self.root / "claims" / f"{self.project}-{stamp}.claimed",
                   {"clientProject": self.project, "slotUTC": stamp, "status": "attempted"})
        self.write(self.root / "claims" / f"{self.project}-{stamp}.result.json",
                   {"clientProject": self.project, "slotUTC": stamp, "status": status})

    def run_monitor(self, **overrides):
        return INCIDENT.monitor(**(self.kwargs | overrides))

    def test_missed_opens_once_and_persists_private_state(self):
        opened = self.run_monitor()
        self.assertEqual(opened["event"], "backup.incident.opened")
        self.assertTrue(opened["incidentActive"])
        self.assertFalse(opened["notificationsDelivered"])
        self.assertFalse(opened["recoveryProven"])
        self.assertEqual(self.run_monitor()["event"], "backup.incident.unchanged")
        state = self.root / "state" / "acme-dev.incident.json"
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(state.read_text())["condition"], "unhealthy")
        self.assertNotIn("age1", state.read_text())

    def test_new_successful_slot_emits_recovery_once_without_restore_claim(self):
        self.run_monitor()
        later = "20261030T120000Z"
        self.receipt(later)
        result = self.run_monitor(now=dt.datetime(2026, 10, 30, 12, 6, tzinfo=UTC))
        self.assertEqual(result["event"], "backup.incident.recovered")
        self.assertFalse(result["incidentActive"])
        self.assertFalse(result["recoveryProven"])
        self.assertEqual(self.run_monitor(now=dt.datetime(2026, 10, 30, 12, 6,
                                                        tzinfo=UTC))["event"],
                         "backup.incident.unchanged")

    def test_same_slot_receipt_change_cannot_falsely_recover(self):
        self.run_monitor()
        self.receipt(self.stamp)
        result = self.run_monitor()
        self.assertEqual(result["event"], "backup.incident.unchanged")
        self.assertTrue(result["incidentActive"])

    def test_successful_baseline_then_corruption_opens_incident(self):
        self.receipt(self.stamp)
        self.assertEqual(self.run_monitor()["event"], "backup.incident.unchanged")
        receipt = self.root / "claims" / f"{self.project}-{self.stamp}.result.json"
        receipt.chmod(0o644)
        self.assertEqual(self.run_monitor()["event"], "backup.incident.opened")

    def test_no_due_slots_is_indeterminate_not_recovery(self):
        early = self.now.replace(minute=4)
        result = self.run_monitor(now=early)
        self.assertEqual(result["event"], "backup.incident.indeterminate")
        self.assertFalse(result["notificationsDelivered"])
        self.assertFalse(list((self.root / "state").iterdir()))
        self.run_monitor()
        result = self.run_monitor(now=early)
        self.assertEqual(result["event"], "backup.incident.indeterminate")
        self.assertTrue(result["incidentActive"])

    def test_mismatched_tenant_environment_key_and_project_refused(self):
        self.run_monitor()
        for changed in ({"project": "other-dev"}, {"slug": "other"},
                        {"environment": "prod"}, {"gcp_project": "other-project-123"},
                        {"recipient": "age1" + "p" * 55},
                        {"confirm_project": "other-dev"}):
            with self.subTest(changed=changed), self.assertRaises(
                    (INCIDENT.IncidentError, INCIDENT.SCHEDULE.ScheduleError)):
                self.run_monitor(**changed)

    def test_state_symlink_hardlink_world_readable_and_corruption_fail_closed(self):
        self.run_monitor()
        state = self.root / "state" / "acme-dev.incident.json"
        original = state.read_text()
        state.chmod(0o644)
        with self.assertRaises(INCIDENT.IncidentError):
            self.run_monitor()
        state.chmod(0o600)
        link = self.root / "extra"
        os.link(state, link)
        with self.assertRaises(INCIDENT.IncidentError):
            self.run_monitor()
        link.unlink()
        state.unlink()
        target = self.root / "target"
        target.write_text(original)
        state.symlink_to(target)
        with self.assertRaises(INCIDENT.IncidentError):
            self.run_monitor()
        self.assertEqual(target.read_text(), original)
        state.unlink()
        state.write_text("{broken")
        state.chmod(0o600)
        with self.assertRaises(INCIDENT.IncidentError):
            self.run_monitor()

    def test_unsafe_lock_and_directory_fail_closed(self):
        lock = self.root / "state" / "acme-dev.incident.lock"
        lock.symlink_to(self.policy)
        with self.assertRaises(OSError):
            self.run_monitor()
        lock.unlink()
        self.root.joinpath("state").chmod(0o755)
        with self.assertRaises(INCIDENT.SCHEDULE.ScheduleError):
            self.run_monitor()

    def test_parallel_lock_contention_fails_without_state_change(self):
        self.run_monitor()
        lock = self.root / "state" / "acme-dev.incident.lock"
        with open(lock, "rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_monitor()

    def test_refuses_gap_that_could_hide_missed_slot(self):
        self.run_monitor()
        later = self.now + dt.timedelta(days=7)
        with self.assertRaises(INCIDENT.IncidentError):
            self.run_monitor(since=later - dt.timedelta(hours=1), now=later)

    def test_private_state_cannot_be_reused_with_different_age_binding(self):
        self.run_monitor()
        state = self.root / "state" / "acme-dev.incident.json"
        doc = json.loads(state.read_text())
        doc["binding"] = "0" * 64
        self.write(state, doc)
        with self.assertRaises(INCIDENT.IncidentError):
            self.run_monitor()

    def test_cli_failure_is_redacted(self):
        args = []
        for key in ("schedule", "claims", "state-dir", "project", "client-slug",
                    "environment", "gcp-project-id", "recipient", "confirm-project",
                    "since-utc"):
            args.extend(["--" + key, "placeholder"])
        with mock.patch.object(INCIDENT, "monitor",
                               side_effect=INCIDENT.IncidentError("secret://do-not-print")), \
             mock.patch("sys.stderr") as stderr:
            self.assertEqual(INCIDENT.main(args), 2)
            self.assertNotIn("do-not-print", str(stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
