"""Offline, adversarial tests for opt-in provider-native backup health emission."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import subprocess
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_backup_cloud_log", ROOT / "scripts" / "client-backup-cloud-log.py"
)
CLOUD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CLOUD)


class BackupCloudLogTests(unittest.TestCase):
    def setUp(self):
        self.kwargs = dict(schedule=pathlib.Path("/unused/schedule.json"),
                           claims=pathlib.Path("/unused/claims"),
                           state_dir=pathlib.Path("/unused/state"),
                           project="acme-dev", slug="acme", environment="dev",
                           gcp_project="sample-project-123",
                           confirm_gcp_project="sample-project-123",
                           recipient="age1" + "q" * 55,
                           confirm_project="acme-dev",
                           since=dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc))
        self.report = {"event": "backup.incident.opened", "clientProject": "acme-dev",
                       "slotUTC": "20261023T120000Z", "incidentActive": True,
                       "recoveryProven": False, "notificationsDelivered": False,
                       "unsafeExtra": "secret://never-log-me"}

    def run_emit(self, report=None, **changes):
        with mock.patch.object(CLOUD.INCIDENT, "monitor", return_value=(
                self.report if report is None else report)) as monitor, \
             mock.patch.object(CLOUD.subprocess, "run", return_value=(
                subprocess.CompletedProcess([], 0))) as run:
            result = CLOUD.emit(**(self.kwargs | changes))
        return result, monitor, run

    def test_unhealthy_level_uses_exact_project_redacted_json_and_bounded_gcloud(self):
        result, monitor, run = self.run_emit()
        self.assertEqual(result["health"], "UNHEALTHY")
        self.assertFalse(result["notificationsDelivered"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["gcloud", "logging", "write", CLOUD.LOG_NAME])
        self.assertIn("--project=sample-project-123", argv)
        self.assertIn("--severity=ERROR", argv)
        self.assertIn("--payload-type=json", argv)
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        payload = json.loads(argv[4])
        self.assertEqual(payload["health"], "UNHEALTHY")
        self.assertEqual(payload["clientProject"], "acme-dev")
        self.assertFalse(payload["recoveryProven"])
        self.assertNotIn("secret://", str(payload))
        self.assertNotIn("age1", str(payload))
        self.assertEqual(monitor.call_count, 1)

    def test_repeated_unhealthy_poll_still_emits_level_signal(self):
        report = self.report | {"event": "backup.incident.unchanged"}
        result, _, run = self.run_emit(report)
        self.assertEqual(result["health"], "UNHEALTHY")
        self.assertEqual(run.call_count, 1)

    def test_recovered_signal_is_health_not_restore_proof(self):
        report = self.report | {"event": "backup.incident.recovered",
                                "incidentActive": False}
        result, _, run = self.run_emit(report)
        self.assertEqual(result["health"], "HEALTHY")
        self.assertIn("--severity=INFO", run.call_args.args[0])
        self.assertFalse(result["recoveryProven"])

    def test_pending_recovery_is_replayed_after_lost_cloud_write_then_acked(self):
        report = self.report | {"event": "backup.incident.unchanged",
                                "incidentActive": False,
                                "slotUTC": "20261030T120000Z",
                                "pendingRecoverySlot": "20261030T120000Z"}
        with mock.patch.object(CLOUD.INCIDENT, "monitor", return_value=report), \
             mock.patch.object(CLOUD.INCIDENT, "ack_recovery") as ack, \
             mock.patch.object(CLOUD.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = CLOUD.emit(**self.kwargs)
        self.assertEqual(result["health"], "HEALTHY")
        self.assertEqual(json.loads(run.call_args.args[0][4])["incidentEvent"],
                         "backup.incident.recovered")
        ack.assert_called_once()
        self.assertEqual(ack.call_args.kwargs["slot"], "20261030T120000Z")

    def test_failed_recovery_cloud_write_never_acks_durable_pending_event(self):
        report = self.report | {"event": "backup.incident.recovered",
                                "incidentActive": False,
                                "slotUTC": "20261030T120000Z",
                                "pendingRecoverySlot": "20261030T120000Z"}
        with mock.patch.object(CLOUD.INCIDENT, "monitor", return_value=report), \
             mock.patch.object(CLOUD.INCIDENT, "ack_recovery") as ack, \
             mock.patch.object(CLOUD.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(CLOUD.CloudLogError):
                CLOUD.emit(**self.kwargs)
        ack.assert_not_called()

    def test_pending_recovery_cannot_be_forged_for_unhealthy_or_wrong_slot(self):
        for changed in ({"incidentActive": True},
                        {"pendingRecoverySlot": "20261130T120000Z"},
                        {"pendingRecoverySlot": "../foreign"}):
            report = self.report | {"event": "backup.incident.unchanged",
                                    "incidentActive": False,
                                    "slotUTC": "20261030T120000Z",
                                    "pendingRecoverySlot": "20261030T120000Z"} | changed
            with self.subTest(changed=changed), \
                 mock.patch.object(CLOUD.INCIDENT, "monitor", return_value=report), \
                 mock.patch.object(CLOUD.subprocess, "run") as run:
                with self.assertRaises(CLOUD.CloudLogError):
                    CLOUD.emit(**self.kwargs)
                run.assert_not_called()

    def test_no_due_slot_is_unknown_not_healthy(self):
        report = {"event": "backup.incident.indeterminate", "clientProject": "acme-dev",
                  "incidentActive": False, "recoveryProven": False,
                  "notificationsDelivered": False}
        result, _, run = self.run_emit(report)
        self.assertEqual(result["health"], "UNKNOWN")
        self.assertIn("--severity=WARNING", run.call_args.args[0])
        self.assertNotIn("slotUTC", json.loads(run.call_args.args[0][4]))

    def test_indeterminate_with_active_incident_stays_unhealthy(self):
        report = {"event": "backup.incident.indeterminate", "clientProject": "acme-dev",
                  "incidentActive": True, "recoveryProven": False,
                  "notificationsDelivered": False}
        result, _, _ = self.run_emit(report)
        self.assertEqual(result["health"], "UNHEALTHY")

    def test_wrong_gcp_project_confirmation_prevents_monitor_and_write(self):
        with mock.patch.object(CLOUD.INCIDENT, "monitor") as monitor, \
             mock.patch.object(CLOUD.subprocess, "run") as run:
            with self.assertRaises(CLOUD.CloudLogError):
                CLOUD.emit(**(self.kwargs | {"confirm_gcp_project": "other-project-123"}))
        monitor.assert_not_called()
        run.assert_not_called()

    def test_wrong_tenant_binding_prevents_cloud_write(self):
        with mock.patch.object(CLOUD.INCIDENT, "monitor") as monitor, \
             mock.patch.object(CLOUD.subprocess, "run") as run:
            with self.assertRaises(CLOUD.INCIDENT.IncidentError):
                CLOUD.emit(**(self.kwargs | {"slug": "other"}))
        monitor.assert_not_called()
        run.assert_not_called()

    def test_malformed_or_cross_tenant_report_never_reaches_gcloud(self):
        for changed in ({"clientProject": "other-dev"},
                        {"notificationsDelivered": True},
                        {"recoveryProven": True},
                        {"incidentActive": "false"},
                        {"slotUTC": "../../oops"},
                        {"event": "backup.incident.refused"}):
            with self.subTest(changed=changed), \
                 mock.patch.object(CLOUD.INCIDENT, "monitor",
                                   return_value=self.report | changed), \
                 mock.patch.object(CLOUD.subprocess, "run") as run:
                with self.assertRaises(CLOUD.CloudLogError):
                    CLOUD.emit(**self.kwargs)
                run.assert_not_called()

    def test_monitor_refusal_does_not_emit_healthy_sample(self):
        with mock.patch.object(CLOUD.INCIDENT, "monitor",
                               side_effect=CLOUD.INCIDENT.IncidentError("secret")), \
             mock.patch.object(CLOUD.subprocess, "run") as run:
            with self.assertRaises(CLOUD.INCIDENT.IncidentError):
                CLOUD.emit(**self.kwargs)
            run.assert_not_called()

    def test_gcloud_failure_and_timeout_are_bounded_and_redacted(self):
        for response in (subprocess.CompletedProcess([], 1),
                         subprocess.TimeoutExpired(["gcloud"], 20)):
            with self.subTest(response=str(type(response))), \
                 mock.patch.object(CLOUD.INCIDENT, "monitor", return_value=self.report), \
                 mock.patch.object(CLOUD.subprocess, "run") as run:
                if isinstance(response, Exception):
                    run.side_effect = response
                else:
                    run.return_value = response
                with self.assertRaises(CLOUD.CloudLogError) as raised:
                    CLOUD.emit(**self.kwargs)
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_invalid_indeterminate_slot_rejected(self):
        report = self.report | {"event": "backup.incident.indeterminate"}
        with mock.patch.object(CLOUD.INCIDENT, "monitor", return_value=report), \
             mock.patch.object(CLOUD.subprocess, "run") as run:
            with self.assertRaises(CLOUD.CloudLogError):
                CLOUD.emit(**self.kwargs)
            run.assert_not_called()


    def test_automatic_monitor_baseline_starts_at_approved_policy_start(self):
        """Never silently discard a missed slot from a new monitoring installation."""
        now = dt.datetime(2026, 10, 27, 23, 0, tzinfo=dt.timezone.utc)
        start = dt.datetime(2026, 10, 23, 0, 0, tzinfo=dt.timezone.utc)
        policy = {"validFrom": "2026-10-23T00:00:00Z"}
        with mock.patch.object(CLOUD.SCHEDULE, "_policy", return_value=policy), \
             mock.patch.object(CLOUD.SCHEDULE, "_private_owned"), \
             mock.patch.object(CLOUD.INCIDENT, "_state", return_value=None):
            since = CLOUD._automatic_since(now=now, **{
                k: self.kwargs[k] for k in ("schedule", "state_dir", "project",
                                            "slug", "environment", "gcp_project", "recipient")})
        self.assertEqual(since, start)

    def test_automatic_monitor_overlaps_last_observed_slot_after_restart(self):
        """An operator timer may restart; its lookback must cover the durable last slot."""
        now = dt.datetime(2026, 10, 27, 23, 0, tzinfo=dt.timezone.utc)
        prior = {"lastSlot": "20261023T120000Z", "condition": "unhealthy"}
        with mock.patch.object(CLOUD.SCHEDULE, "_policy", return_value={
                 "validFrom": "2026-10-20T00:00:00Z"}), \
             mock.patch.object(CLOUD.SCHEDULE, "_private_owned"), \
             mock.patch.object(CLOUD.INCIDENT, "_state", return_value=prior):
            since = CLOUD._automatic_since(now=now, **{
                k: self.kwargs[k] for k in ("schedule", "state_dir", "project",
                                            "slug", "environment", "gcp_project", "recipient")})
        self.assertEqual(since, dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc))

    def test_automatic_monitor_refuses_expired_lookback_and_unsafe_state(self):
        """No healthy Cloud Logging sample can be emitted after an unobserved 31-day gap."""
        now = dt.datetime(2026, 10, 27, 23, tzinfo=dt.timezone.utc)
        params = {k: self.kwargs[k] for k in ("schedule", "state_dir", "project",
                                             "slug", "environment", "gcp_project", "recipient")}
        with mock.patch.object(CLOUD.SCHEDULE, "_policy", return_value={
                 "validFrom": "2026-09-01T00:00:00Z"}), \
             mock.patch.object(CLOUD.SCHEDULE, "_private_owned"), \
             mock.patch.object(CLOUD.INCIDENT, "_state", return_value=None):
            with self.assertRaises(CLOUD.CloudLogError):
                CLOUD._automatic_since(now=now, **params)
        with mock.patch.object(CLOUD.SCHEDULE, "_policy", return_value={
                 "validFrom": "2026-10-20T00:00:00Z"}), \
             mock.patch.object(CLOUD.SCHEDULE, "_private_owned"), \
             mock.patch.object(CLOUD.INCIDENT, "_state", side_effect=CLOUD.INCIDENT.IncidentError("unsafe state")):
            with self.assertRaises(CLOUD.INCIDENT.IncidentError):
                CLOUD._automatic_since(now=now, **params)

    def test_emit_automatic_since_is_derived_before_any_cloud_write(self):
        chosen = dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc)
        with mock.patch.object(CLOUD, "_automatic_since", return_value=chosen) as automatic, \
             mock.patch.object(CLOUD.INCIDENT, "monitor", return_value=self.report) as monitor, \
             mock.patch.object(CLOUD.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)):
            CLOUD.emit(**(self.kwargs | {"since": None}))
        automatic.assert_called_once()
        self.assertEqual(monitor.call_args.kwargs["since"], chosen)

    def test_cli_requires_exactly_one_manual_or_contiguous_auto_lookback(self):
        flags = ["--schedule", "/unused/schedule.json", "--claims", "/unused/claims",
                 "--state-dir", "/unused/state", "--project", "acme-dev",
                 "--client-slug", "acme", "--environment", "dev",
                 "--gcp-project-id", "sample-project-123",
                 "--confirm-gcp-project-id", "sample-project-123",
                 "--recipient", "age1" + "q" * 55, "--confirm-project", "acme-dev"]
        with mock.patch.object(CLOUD, "emit", return_value={
                 "health": "UNKNOWN"}) as emit:
            self.assertEqual(CLOUD.main(flags + ["--auto-since"]), 3)
            self.assertIsNone(emit.call_args.kwargs["since"])
        with self.assertRaises(SystemExit):
            CLOUD.main(flags)
        with self.assertRaises(SystemExit):
            CLOUD.main(flags + ["--auto-since", "--since-utc", "2026-10-23T00:00:00Z"])



if __name__ == "__main__":
    unittest.main()
