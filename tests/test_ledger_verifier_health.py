"""Credential-free independent verifier level and publication regressions (#80)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ledger_verifier_health", ROOT / "scripts/ledger-verifier-health.py")
cli = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = cli
spec.loader.exec_module(cli)


class FakeMonitor:
    def __init__(self, root, name):
        self.root, self.name = root, name
        self.failure = None
        self.evaluated = []
        self.report = {"ok": True, "problems": [], "localChainedEvents": 2,
                       "exportedRecords": 2, "exportedHeadSequence": 2}
        self.notices = []

    def check(self, *args, **kwargs):
        if self.failure:
            raise self.failure
        return {"report": self.report, "notices": self.notices}

    def evaluate(self, report, **kwargs):
        self.evaluated.append((report, kwargs))
        return []


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (pathlib.Path(self.tmp.name) / "state").mkdir(mode=0o700)
        self.monitor = FakeMonitor(pathlib.Path(self.tmp.name), "acme-dev-mg")
        self.sent = []
        self.kwargs = dict(ledger=pathlib.Path(self.tmp.name) / "audit.jsonl",
            state_dir=pathlib.Path(self.tmp.name) / "state", bucket="locked-ledger-bucket",
            prefix="acme/dev", client_project="acme-dev-mg", confirm_client_project="acme-dev-mg",
            gcp_project="client-gcp-123", confirm_gcp_project="client-gcp-123",
            verifier_identity="auditor@client-gcp-123.iam.gserviceaccount.com",
            runtime_identity="runtime@client-gcp-123.iam.gserviceaccount.com",
            now=1793000000, monitor_factory=lambda *a, **kw: self.monitor,
            sink_factory=lambda: object(), publish=self.publish)

    def publish(self, command):
        self.sent.append(command)
        return 0

    def payload(self):
        self.assertEqual(len(self.sent), 1)
        cmd = self.sent[0]
        self.assertEqual(cmd[:4], ["gcloud", "logging", "write", "model-garden-ledger-verifier"])
        self.assertEqual(cmd[5:7], ["--project=client-gcp-123", "--payload-type=json"])
        return json.loads(cmd[4])

    def test_healthy_verified_chain_emits_level_without_delivery_claim(self):
        result = cli.check_and_publish(**self.kwargs)
        self.assertEqual(result["health"], "HEALTHY")
        self.assertFalse(result["notificationsDelivered"])
        self.assertTrue(self.payload()["independentVerificationPassed"])
        self.assertIn("--severity=INFO", self.sent[0])

    def test_real_empty_ledger_export_report_is_not_healthy(self):
        export = cli._load("test_empty_verifier_ledger_export", "platform/core/ledger_export.py")
        report = export.verify_export(
            self.kwargs["ledger"],
            export.DirectorySink(pathlib.Path(self.tmp.name) / "empty-remote-export"),
        )
        # The generic chain verifier is intentionally a consistency checker;
        # the independent HEALTHY signal has the stronger nonempty-evidence gate.
        self.assertTrue(report["ok"])
        self.assertEqual(report["exportedRecords"], 0)
        self.monitor.report = report
        result = cli.check_and_publish(**self.kwargs)
        self.assertEqual(result["health"], "UNHEALTHY")
        self.assertEqual(self.payload()["conditions"], ["export-failure"])

    def test_empty_or_unsubstantiated_export_never_reports_healthy(self):
        # A brand-new or missing ledger and empty bucket must not be
        # independently certified as healthy merely because both agree.
        for report in (
            {"ok": True, "problems": [], "localChainedEvents": 0,
             "exportedRecords": 0, "exportedHeadSequence": 0},
            {"ok": True, "problems": []},
            {"ok": True, "problems": [], "localChainedEvents": 1,
             "exportedRecords": 0, "exportedHeadSequence": 0},
            {"ok": True, "problems": [], "localChainedEvents": 1,
             "exportedRecords": True, "exportedHeadSequence": 1},
            {"ok": True, "problems": [], "localChainedEvents": 1,
             "exportedRecords": 1, "exportedHeadSequence": 0},
        ):
            with self.subTest(report=report):
                self.sent.clear()
                self.monitor.evaluated.clear()
                self.monitor.report = report
                result = cli.check_and_publish(**self.kwargs)
                self.assertEqual(result["health"], "UNHEALTHY")
                self.assertFalse(result["independentVerificationPassed"])
                self.assertEqual(self.payload()["conditions"], ["export-failure"])
                self.assertEqual(self.monitor.evaluated[0][0]["ok"], False)
                self.assertIn("--severity=ERROR", self.sent[0])

    def test_corrupt_export_is_unhealthy_and_redacted(self):
        self.monitor.report = {"ok": False, "problems": ["secret credential and customer content"]}
        self.monitor.notices = [{"kind": "export-integrity", "text": "sensitive"}]
        result = cli.check_and_publish(**self.kwargs)
        self.assertEqual(result["health"], "UNHEALTHY")
        payload = self.payload()
        self.assertEqual(payload["conditions"], ["export-integrity"])
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("sensitive", str(payload))
        self.assertIn("--severity=ERROR", self.sent[0])

    def test_verifier_exception_never_emits_healthy_and_is_monitored(self):
        self.monitor.failure = RuntimeError("secret token from provider")
        result = cli.check_and_publish(**self.kwargs)
        self.assertFalse(result["independentVerificationPassed"])
        self.assertEqual(self.payload()["conditions"], ["export-failure"])
        self.assertEqual(self.monitor.evaluated[0][0]["ok"], False)
        self.assertNotIn("secret", str(self.sent))

    def test_wrong_tenant_and_shared_identity_refuse_before_verification_or_network(self):
        for change in ({"confirm_client_project": "other-dev"},
                       {"confirm_gcp_project": "other-gcp"},
                       {"verifier_identity": self.kwargs["runtime_identity"]},
                       {"prefix": "../foreign"},
                       {"bucket": "https://attacker.test"},
                       {"client_project": "other/dev"}):
            with self.subTest(change=change):
                self.sent.clear()
                with self.assertRaises(cli.VerifierError):
                    cli.check_and_publish(**(self.kwargs | change))
                self.assertEqual(self.sent, [])

    def test_unsafe_state_symlink_and_world_readable_directory_fail_closed(self):
        state = self.kwargs["state_dir"]
        state.rmdir()
        state.symlink_to(pathlib.Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(cli.VerifierError):
            cli.check_and_publish(**self.kwargs)
        self.assertEqual(self.sent, [])
        state.unlink()
        state.mkdir(mode=0o755)
        with self.assertRaises(cli.VerifierError):
            cli.check_and_publish(**self.kwargs)
        self.assertEqual(self.sent, [])

    def test_cloud_logging_failure_is_not_a_delivery_receipt(self):
        with self.assertRaisesRegex(cli.VerifierError, "rejected"):
            cli.check_and_publish(**(self.kwargs | {"publish": lambda cmd: 1}))
        self.assertEqual(self.monitor.evaluated, [])

    def test_bounded_cloud_logging_invocation_uses_no_shell_or_output(self):
        with mock.patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            cli.check_and_publish(**(self.kwargs | {"publish": None}))
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
