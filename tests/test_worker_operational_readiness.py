"""Liveness and operational readiness are separate for the governed worker (#231).

No client payload, provider credentials, or live infrastructure is needed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("test_worker_operational_module", ROOT / "platform/runtime/worker.py")
worker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker)

T0 = 1_800_000_000


class WorkerOperationalReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "worker" / "heartbeat.json"

    def write(self, body: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(body), encoding="utf-8")

    def test_rejects_missing_corrupt_stale_future_and_non_numeric_heartbeats(self) -> None:
        self.assertEqual(worker.operational_health(self.path, now=T0)["status"], "unavailable")
        cases = [
            ({"at": T0 - 301, "failed": []}, "stale"),
            ({"at": T0 + 31, "failed": []}, "future"),
            ({"at": True, "failed": []}, "invalid"),
            ({"at": "1800000000", "failed": []}, "invalid"),
            ({"at": T0, "failed": "secrets"}, "invalid"),
            ({"at": T0, "failed": [], "persistentFailures": ["secret=abc!"]}, "invalid"),
        ]
        for source, expected_reason in cases:
            with self.subTest(source=source):
                self.write(source)
                report = worker.operational_health(self.path, now=T0)
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["reason"], expected_reason)
                self.assertFalse(worker.healthy(self.path, now=T0))
        self.path.write_text("{ broken", encoding="utf-8")
        self.assertEqual(worker.operational_health(self.path, now=T0)["status"], "unavailable")

    def test_persistent_job_failure_is_visible_but_does_not_kill_liveness(self) -> None:
        runtime = worker.Worker.__new__(worker.Worker)
        runtime.heartbeat = self.path
        runtime._failure_streaks = {}
        for attempt in (1, 2, 3):
            runtime._beat(T0 + attempt, {"scheduler": {"failed": "RuntimeError"}, "notices": {"sent": 1}})
            body = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(body["failed"], ["scheduler"])
            self.assertEqual(body["persistentFailures"], ["scheduler"] if attempt >= 3 else [])
            status = worker.operational_health(self.path, now=T0 + attempt)
            self.assertEqual(status["status"], "degraded" if attempt >= 3 else "healthy")
            self.assertTrue(worker.healthy(self.path, now=T0 + attempt))
        # On ticks where a long-interval job is not run, its failure remains observable.
        runtime._beat(T0 + 5, {"notices": {"sent": 0}})
        self.assertEqual(worker.operational_health(self.path, now=T0 + 5)["status"], "degraded")
        # A successful attempt clears the condition.
        runtime._beat(T0 + 6, {"scheduler": {"runs": 0}})
        report = worker.operational_health(self.path, now=T0 + 6)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["persistentFailures"], [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_payload_leaks_to_health_output_and_readiness_exits_failure(self) -> None:
        runtime = worker.Worker.__new__(worker.Worker)
        runtime.heartbeat = self.path
        runtime._failure_streaks = {}
        for index in range(3):
            runtime._beat(T0 + index, {"mailbox": {"failed": "customer secret payload"}})
        body = self.path.read_text(encoding="utf-8")
        self.assertNotIn("customer secret", body)
        with mock.patch.object(worker.time, "time", return_value=T0 + 2):
            self.assertEqual(worker.main(["--readiness", "--data-root", str(self.path.parent.parent)]), 1)
            self.assertEqual(worker.main(["--health", "--data-root", str(self.path.parent.parent)]), 0)


if __name__ == "__main__":
    unittest.main()
