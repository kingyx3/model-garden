"""Fail-closed export failure/recovery alert regressions (#80).

A configured export attempt that fails with zero measurable lag must not be
reported healthy or silently skipped. No GCP credentials or tenant data.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = load("worker_export_failure_test", "platform/runtime/worker.py")
monitor_module = load("monitor_export_failure_test", "platform/core/export_monitor.py")
ledger = worker._module("ledger")
NOW = 1_790_000_000
OWNER = "owner@client.example"


class ExportFailureAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.runtime = worker.Worker.__new__(worker.Worker)
        self.runtime.data = root
        self.runtime.audit_path = root / "audit.jsonl"
        self.runtime.audit_path.write_text("", encoding="utf-8")
        self.runtime.config = {"export": {"gcsBucket": "not-a-real-bucket"}}
        self.runtime.export_sink = object()
        self.runtime._export_lag = lambda: 0
        self.monitor = monitor_module.ExportMonitor(root / "monitor")

    def notices(self, at=NOW):
        report = self.runtime._export_health()["report"]
        return self.monitor.evaluate(report, now=at, owner=OWNER)

    def test_failed_export_without_any_lag_alerts_and_never_leaks_provider_error(self):
        with mock.patch.object(ledger, "export_ledger", side_effect=OSError("token for private customer")):
            with self.assertRaises(OSError):
                self.runtime.run_export(NOW)
        report = self.runtime._export_health()["report"]
        self.assertEqual(report["lagEvents"], 0)
        self.assertTrue(any(problem.startswith("export failure:") for problem in report["problems"]))
        first = self.notices()
        self.assertEqual([notice["kind"] for notice in first], ["export-failure"])
        self.assertNotIn("private customer", str(first))
        self.assertIn("cannot confirm", first[0]["text"])
        self.monitor.mark_notified(first[0], via="inbox", now=NOW)
        self.assertEqual(self.notices(NOW + 60), [])

    def test_repeated_failures_do_not_claim_recovery_and_success_does(self):
        for at in (NOW, NOW + 60):
            with mock.patch.object(ledger, "export_ledger", side_effect=TimeoutError("provider timed out")):
                with self.assertRaises(TimeoutError):
                    self.runtime.run_export(at)
            notices = self.notices(at)
            if at == NOW:
                self.monitor.mark_notified(notices[0], via="inbox", now=at)
            else:
                self.assertEqual(notices, [])
        with mock.patch.object(ledger, "export_ledger", return_value={"exportedRecords": 0}):
            self.assertEqual(self.runtime.run_export(NOW + 120), {"exportedRecords": 0})
        self.assertEqual([notice["kind"] for notice in self.notices(NOW + 120)], ["export-recovered"])

    def test_failed_verifier_report_with_no_problem_details_is_not_healthy(self):
        notices = self.monitor.evaluate({"ok": False, "problems": [], "lagEvents": 0}, now=NOW, owner=OWNER)
        self.assertEqual([notice["kind"] for notice in notices], ["export-failure"])
        self.assertNotIn("healthy", notices[0]["text"].lower())


if __name__ == "__main__":
    unittest.main()
