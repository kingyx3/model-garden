"""Credential-free, fail-closed tests for preapproved recurring backup dispatch (#230)."""
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
SPEC = importlib.util.spec_from_file_location("client_backup_schedule", ROOT / "scripts" / "client-backup-schedule.py")
SCHEDULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCHEDULE)


class BackupScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for name in ("policy", "approvals", "claims", "output"):
            (self.root / name).mkdir(mode=0o700)
        self.now = dt.datetime(2026, 10, 23, 12, 0, 30, tzinfo=dt.timezone.utc)
        self.slot = self.now.replace(second=0)
        self.recipient = "age1" + "q" * 55
        self.image = "docker.io/library/busybox@sha256:" + "a" * 64
        self.policy = self.root / "policy" / "schedule.json"
        self.approval = self.root / "approvals" / "acme-dev-20261023T120000Z.json"
        binding = {
            "schemaVersion": 1, "clientProject": "acme-dev", "clientSlug": "acme",
            "environment": "dev", "gcpProjectId": "sample-project-123",
            "ageRecipientSha256": hashlib.sha256(self.recipient.encode()).hexdigest(),
            "approvedBy": "operator-1", "changeRef": "CHG-123",
        }
        self.policy_doc = dict(binding, validFrom="2026-10-20T00:00:00Z",
                               expiresAt="2026-11-15T00:00:00Z",
                               weekdaysUTC=[4], hourUTC=12, minuteUTC=0)
        self.approval_doc = dict(binding, validFrom="2026-10-23T12:00:00Z",
                                 expiresAt="2026-10-23T12:30:00Z")
        self.write(self.policy, self.policy_doc)
        self.write(self.approval, self.approval_doc)
        self.kwargs = dict(schedule=self.policy, approvals=self.root / "approvals",
                           claims=self.root / "claims", compose=self.root / "compose.yaml",
                           project="acme-dev", slug="acme", environment="dev",
                           gcp_project="sample-project-123", recipient=self.recipient,
                           image=self.image, output_parent=self.root / "output",
                           confirm_project="acme-dev", now=self.now)

    def write(self, path, obj):
        path.write_text(json.dumps(obj), encoding="utf-8")
        path.chmod(0o600)

    def execute(self, **overrides):
        with mock.patch.object(SCHEDULE.WINDOW, "run", return_value={
            "status": "uploaded-not-recovery-proven", "runId": "acme-dev-123"}) as run:
            result = SCHEDULE.dispatch(**(self.kwargs | overrides))
        return result, run

    def test_approved_occurrence_dispatches_once_and_claims_durably(self):
        result, run = self.execute()
        self.assertEqual(result["event"], "backup.schedule.dispatched")
        self.assertEqual(result["status"], "uploaded-not-recovery-proven")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["approval"], self.approval)
        claim = self.root / "claims" / "acme-dev-20261023T120000Z.claimed"
        self.assertEqual(json.loads(claim.read_text())["status"], "attempted")
        self.assertEqual(claim.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute()

    def test_outside_utc_slot_and_weekday_skip_without_claim(self):
        for moment in (self.now - dt.timedelta(minutes=1),
                       self.now + dt.timedelta(minutes=5),
                       self.now + dt.timedelta(days=1)):
            result, run = self.execute(now=moment)
            self.assertEqual(result["event"], "backup.schedule.skipped")
            run.assert_not_called()
        self.assertEqual(list((self.root / "claims").iterdir()), [])

    def test_wrong_tenant_environment_cloud_key_or_confirmation_refused(self):
        for changed in ({"project": "other-dev"}, {"slug": "other"},
                        {"environment": "prod"}, {"gcp_project": "other-project-123"},
                        {"recipient": "age1" + "p" * 55}, {"confirm_project": "other-dev"}):
            with self.subTest(changed=changed), self.assertRaises(SCHEDULE.ScheduleError):
                self.execute(**changed)
        self.assertEqual(list((self.root / "claims").iterdir()), [])

    def test_missing_wrong_slot_or_mismatched_approval_refused_without_claim(self):
        self.approval.unlink()
        with self.assertRaises(SCHEDULE.ScheduleError):
            self.execute()
        self.approval_doc["validFrom"] = "2026-10-23T11:00:00Z"
        self.write(self.approval, self.approval_doc)
        with self.assertRaises(SCHEDULE.ScheduleError):
            self.execute()
        self.approval_doc["validFrom"] = "2026-10-23T12:00:00Z"
        self.approval_doc["changeRef"] = "CHG-OTHER"
        self.write(self.approval, self.approval_doc)
        with self.assertRaises(SCHEDULE.ScheduleError):
            self.execute()
        self.assertEqual(list((self.root / "claims").iterdir()), [])

    def test_unbounded_policy_duplicate_weekdays_and_bool_values_refused(self):
        for changes in ({"expiresAt": "2026-12-31T00:00:00Z"},
                        {"weekdaysUTC": [4, 4]}, {"weekdaysUTC": [True]},
                        {"hourUTC": True}, {"minuteUTC": 60},
                        {"schemaVersion": True}):
            saved = self.policy_doc.copy()
            self.policy_doc.update(changes)
            self.write(self.policy, self.policy_doc)
            with self.subTest(changes=changes), self.assertRaises(SCHEDULE.ScheduleError):
                self.execute()
            self.policy_doc = saved
        self.assertEqual(list((self.root / "claims").iterdir()), [])

    def test_symlink_or_insecure_policy_and_claim_directories_fail_closed(self):
        self.policy.chmod(0o644)
        with self.assertRaises(SCHEDULE.ScheduleError):
            self.execute()
        self.policy.chmod(0o600)
        self.policy.rename(self.policy.with_suffix(".old"))
        self.policy.symlink_to(self.policy.with_suffix(".old"))
        with self.assertRaises(SCHEDULE.ScheduleError):
            self.execute()
        self.policy.unlink()
        self.write(self.policy, self.policy_doc)
        (self.root / "claims").chmod(0o755)
        with self.assertRaises(SCHEDULE.ScheduleError):
            self.execute()
        self.assertEqual(list((self.root / "claims").iterdir()), [])

    def test_failure_after_claim_does_not_retry(self):
        with mock.patch.object(SCHEDULE.WINDOW, "run", side_effect=SCHEDULE.WINDOW.WindowError("upload failed")):
            with self.assertRaises(SCHEDULE.WINDOW.WindowError):
                SCHEDULE.dispatch(**self.kwargs)
        self.assertEqual(len(list((self.root / "claims").iterdir())), 1)
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute()

    def test_cli_redacts_exception_details(self):
        with mock.patch.object(SCHEDULE, "dispatch", side_effect=SCHEDULE.ScheduleError("secret://must-not-log")), \
             mock.patch("sys.stderr") as stderr:
            args = []
            for name in ("schedule", "approvals", "claims", "compose", "project", "client-slug",
                         "environment", "gcp-project-id", "recipient", "image", "output-parent", "confirm-project"):
                args.extend(["--" + name, "placeholder"])
            self.assertEqual(SCHEDULE.main(args), 2)
            self.assertNotIn("must-not-log", str(stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
