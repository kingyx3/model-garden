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


if __name__ == "__main__":
    unittest.main()
