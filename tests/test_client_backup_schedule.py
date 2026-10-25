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
        self.assertEqual(run.call_args.kwargs["not_after"],
                         dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc))
        claim = self.root / "claims" / "acme-dev-20261023T120000Z.claimed"
        self.assertEqual(json.loads(claim.read_text())["status"], "attempted")
        self.assertEqual(claim.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute()

    def test_dispatch_caps_long_occurrence_at_short_recurrence_expiry(self):
        self.policy_doc["expiresAt"] = "2026-10-23T12:02:00Z"
        self.write(self.policy, self.policy_doc)
        result, run = self.execute()
        self.assertEqual(result["event"], "backup.schedule.dispatched")
        self.assertEqual(run.call_args.kwargs["not_after"],
                         dt.datetime(2026, 10, 23, 12, 2, tzinfo=dt.timezone.utc))
        self.assertNotEqual(run.call_args.kwargs["not_after"],
                            SCHEDULE.WINDOW._utc(self.approval_doc["expiresAt"]))

    def test_approved_friday_slot_runs_after_utc_midnight_once(self):
        # Friday 23:59 UTC is still due on Saturday 00:02 UTC. The slot
        # identity, approval and irreversible claim must remain Friday's.
        self.policy_doc.update(hourUTC=23, minuteUTC=59)
        self.write(self.policy, self.policy_doc)
        self.approval.unlink()
        self.approval = self.root / "approvals" / "acme-dev-20261023T235900Z.json"
        self.approval_doc.update(validFrom="2026-10-23T23:59:00Z",
                                 expiresAt="2026-10-24T00:20:00Z")
        self.write(self.approval, self.approval_doc)
        moment = dt.datetime(2026, 10, 24, 0, 2, tzinfo=dt.timezone.utc)
        result, run = self.execute(now=moment)
        self.assertEqual(result["slotUTC"], "20261023T235900Z")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["approval"], self.approval)
        claim = self.root / "claims" / "acme-dev-20261023T235900Z.claimed"
        receipt = self.root / "claims" / "acme-dev-20261023T235900Z.result.json"
        self.assertEqual(json.loads(claim.read_text())["slotUTC"], "20261023T235900Z")
        self.assertEqual(json.loads(receipt.read_text())["status"],
                         "uploaded-not-recovery-proven")
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute(now=moment)

    def test_cross_midnight_grace_refuses_wrong_weekday_and_policy_bounds(self):
        policy = dict(self.policy_doc, hourUTC=23, minuteUTC=59)
        moment = dt.datetime(2026, 10, 24, 0, 2, tzinfo=dt.timezone.utc)
        expected = dt.datetime(2026, 10, 23, 23, 59, tzinfo=dt.timezone.utc)
        self.assertEqual(SCHEDULE._slot(policy, moment), expected)
        self.assertEqual(SCHEDULE._slot(policy, moment.replace(minute=3, second=59)),
                         expected)
        # At exactly five minutes, the grace period is closed.
        self.assertIsNone(SCHEDULE._slot(policy, moment.replace(minute=4)))
        self.assertIsNone(SCHEDULE._slot(dict(policy, weekdaysUTC=[5]), moment))
        self.assertIsNone(SCHEDULE._slot(dict(policy, validFrom="2026-10-24T00:00:00Z"),
                                         moment))
        self.assertIsNone(SCHEDULE._slot(dict(policy, expiresAt="2026-10-23T23:59:00Z"),
                                         moment))

    def test_recurring_authorization_expires_during_grace_without_claim(self):
        # A still-valid per-occurrence approval cannot extend the recurrence.
        self.policy_doc["expiresAt"] = "2026-10-23T12:01:00Z"
        self.write(self.policy, self.policy_doc)
        before_expiry = dt.datetime(2026, 10, 23, 12, 0, 59,
                                    tzinfo=dt.timezone.utc)
        self.assertEqual(SCHEDULE._slot(self.policy_doc, before_expiry), self.slot)
        at_expiry = dt.datetime(2026, 10, 23, 12, 1, tzinfo=dt.timezone.utc)
        self.assertIsNone(SCHEDULE._slot(self.policy_doc, at_expiry))
        self.assertIsNone(SCHEDULE._slot(self.policy_doc, at_expiry +
                                         dt.timedelta(seconds=30)))
        result, run = self.execute(now=at_expiry)
        self.assertEqual(result["event"], "backup.schedule.skipped")
        run.assert_not_called()
        self.assertEqual(list((self.root / "claims").iterdir()), [])

    def test_dispatch_before_policy_expiry_still_requires_approval(self):
        self.policy_doc["expiresAt"] = "2026-10-23T12:01:00Z"
        self.write(self.policy, self.policy_doc)
        moment = dt.datetime(2026, 10, 23, 12, 0, 59,
                             tzinfo=dt.timezone.utc)
        result, run = self.execute(now=moment)
        self.assertEqual(result["event"], "backup.schedule.dispatched")
        run.assert_called_once()
        # The same timer tick at expiry must skip even if the earlier
        # invocation already claimed the slot and the occurrence is valid.
        result, run = self.execute(now=moment + dt.timedelta(seconds=1))
        self.assertEqual(result["event"], "backup.schedule.skipped")
        run.assert_not_called()

    def test_previous_day_grace_cannot_outlive_recurring_authorization(self):
        policy = dict(self.policy_doc, hourUTC=23, minuteUTC=59,
                      expiresAt="2026-10-24T00:01:00Z")
        before = dt.datetime(2026, 10, 24, 0, 0, 59,
                             tzinfo=dt.timezone.utc)
        slot = dt.datetime(2026, 10, 23, 23, 59,
                           tzinfo=dt.timezone.utc)
        self.assertEqual(SCHEDULE._slot(policy, before), slot)
        self.assertIsNone(SCHEDULE._slot(
            policy, before + dt.timedelta(seconds=1)))
        self.assertIsNone(SCHEDULE._slot(
            policy, before + dt.timedelta(minutes=2)))

    def test_previous_utc_slot_across_year_boundary_and_timezone(self):
        policy = dict(self.policy_doc, validFrom="2026-12-30T00:00:00Z",
                      expiresAt="2027-01-03T00:00:00Z", weekdaysUTC=[3],
                      hourUTC=23, minuteUTC=59)
        now = dt.datetime(2027, 1, 1, 0, 1, tzinfo=dt.timezone.utc)
        expected = dt.datetime(2026, 12, 31, 23, 59, tzinfo=dt.timezone.utc)
        self.assertEqual(SCHEDULE._slot(policy, now), expected)
        self.assertEqual(SCHEDULE._slot(policy, now.astimezone(dt.timezone(dt.timedelta(hours=8)))),
                         expected)

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
        self.assertEqual(len(list((self.root / "claims").iterdir())), 2)
        receipt = self.root / "claims" / "acme-dev-20261023T120000Z.result.json"
        self.assertEqual(json.loads(receipt.read_text())["status"], "failed-not-recovery-proven")
        self.assertNotIn("upload failed", receipt.read_text())
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute()

    def test_success_has_durable_private_result_receipt_without_recovery_claim(self):
        result, _ = self.execute()
        self.assertEqual(result["status"], "uploaded-not-recovery-proven")
        receipt = self.root / "claims" / "acme-dev-20261023T120000Z.result.json"
        self.assertEqual(json.loads(receipt.read_text()), {
            "clientProject": "acme-dev", "slotUTC": "20261023T120000Z",
            "status": "uploaded-not-recovery-proven",
        })
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute()

    def test_preexisting_result_receipt_is_not_overwritten(self):
        receipt = self.root / "claims" / "acme-dev-20261023T120000Z.result.json"
        self.write(receipt, {"sentinel": True})
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "record backup result"):
            self.execute()
        self.assertEqual(json.loads(receipt.read_text()), {"sentinel": True})
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute()

    def test_symlinked_result_receipt_is_not_followed(self):
        target = self.root / "claims" / "operator-data.json"
        self.write(target, {"sentinel": True})
        receipt = self.root / "claims" / "acme-dev-20261023T120000Z.result.json"
        receipt.symlink_to(target)
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "record backup result"):
            self.execute()
        self.assertEqual(json.loads(target.read_text()), {"sentinel": True})
        with self.assertRaisesRegex(SCHEDULE.ScheduleError, "already attempted"):
            self.execute()

    def test_replaced_recurrence_during_slot_selection_refused_before_claim(self):
        original_slot = SCHEDULE._slot

        def replace_policy(doc, now):
            self.policy.rename(self.policy.with_suffix(".old"))
            self.write(self.policy, self.policy_doc)
            return original_slot(doc, now)

        with mock.patch.object(SCHEDULE, "_slot", side_effect=replace_policy):
            with self.assertRaisesRegex(SCHEDULE.ScheduleError, "recurring authorization"):
                self.execute()
        self.assertEqual(list((self.root / "claims").iterdir()), [])

    def test_recurrence_revoked_during_window_blocks_later_stages(self):
        def revoked(**kwargs):
            self.policy.unlink()
            kwargs["authorization_guard"]()
            return {"status": "uploaded-not-recovery-proven", "runId": "mock"}

        with mock.patch.object(SCHEDULE.WINDOW, "run", side_effect=revoked):
            with self.assertRaisesRegex(SCHEDULE.ScheduleError, "recurring authorization"):
                SCHEDULE.dispatch(**self.kwargs)
        receipt = self.root / "claims" / "acme-dev-20261023T120000Z.result.json"
        self.assertEqual(json.loads(receipt.read_text())["status"], "failed-not-recovery-proven")

    def test_in_place_recurrence_change_after_upload_never_reports_success(self):
        def changed(**kwargs):
            kwargs["authorization_guard"]()
            self.policy_doc["expiresAt"] = "2026-11-16T00:00:00Z"
            self.write(self.policy, self.policy_doc)
            return {"status": "uploaded-not-recovery-proven", "runId": "mock"}

        with mock.patch.object(SCHEDULE.WINDOW, "run", side_effect=changed):
            with self.assertRaisesRegex(SCHEDULE.ScheduleError, "recurring authorization"):
                SCHEDULE.dispatch(**self.kwargs)
        receipt = self.root / "claims" / "acme-dev-20261023T120000Z.result.json"
        self.assertEqual(json.loads(receipt.read_text())["status"], "failed-not-recovery-proven")

    def test_unchanged_recurrence_guard_allows_success(self):
        def permitted(**kwargs):
            kwargs["authorization_guard"]()
            return {"status": "uploaded-not-recovery-proven", "runId": "mock"}

        with mock.patch.object(SCHEDULE.WINDOW, "run", side_effect=permitted):
            result = SCHEDULE.dispatch(**self.kwargs)
        self.assertEqual(result["status"], "uploaded-not-recovery-proven")

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
