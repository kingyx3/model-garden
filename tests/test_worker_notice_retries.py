"""Regression: owner alerts must survive a saturated notice queue and retry after restart."""
from __future__ import annotations

import calendar
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("model_garden_worker_notice_retry", ROOT / "platform/runtime/worker.py")
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)
NOW = calendar.timegm((2026, 10, 27, 12, 0, 0))


class OwnerNoticeRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = pathlib.Path(temporary.name)
        self.runtime = self.make_runtime()

    def make_runtime(self):
        runtime = worker.Worker.__new__(worker.Worker)
        runtime.data = self.data
        runtime.queue = worker.NoticeQueue(self.data / "worker")
        runtime.config = {"spend": {"capCents": 1, "warnAtPercent": [80]},
                          "escalateTo": "owner@example.test", "mailbox": None}
        runtime.audit = lambda *args: None
        runtime.month_to_date_micros = lambda window: 10_000
        return runtime

    def clear_pending(self):
        with self.runtime.queue._locked() as state:
            state["pending"].clear()

    def test_full_queue_does_not_consume_once_only_notification(self):
        with mock.patch.object(worker, "MAX_QUEUE", 1):
            self.runtime.queue.add("other", "owner@example.test", "other", reference="other")
            with self.assertRaisesRegex(RuntimeError, "full"):
                self.runtime._queue_once("knowledge-review", "hours|2026-10-27|stale",
                                         "owner@example.test", "Review the source", notice_reference="hours:stale")
            self.assertFalse((self.data / "worker" / "told.json").exists())
            self.clear_pending()
            self.assertTrue(self.runtime._queue_once("knowledge-review", "hours|2026-10-27|stale",
                                                      "owner@example.test", "Review the source", notice_reference="hours:stale"))
            self.assertFalse(self.runtime._queue_once("knowledge-review", "hours|2026-10-27|stale",
                                                       "owner@example.test", "Review the source", notice_reference="hours:stale"))
            self.assertEqual([n["kind"] for n in self.runtime.queue.pending()], ["knowledge-review"])

    def test_restarted_workers_cannot_enqueue_the_same_once_only_notice_twice(self):
        other = self.make_runtime()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda runtime: runtime._queue_once(
                "task-report", "task123|done", "owner@example.test", "Task finished", notice_reference="task123"),
                (self.runtime, other)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.runtime.queue.pending()), 1)
        self.clear_pending()
        self.assertFalse(other._queue_once("task-report", "task123|done",
                                           "owner@example.test", "Task finished", notice_reference="task123"))

    def test_spend_cap_notice_retries_after_pause_when_queue_was_full(self):
        with mock.patch.object(worker, "MAX_QUEUE", 1):
            self.runtime.queue.add("other", "owner@example.test", "other", reference="other")
            with self.assertRaisesRegex(RuntimeError, "full"):
                self.runtime.run_spend(NOW)
            self.assertTrue(worker._module("pause").PauseStore(self.data).status()["paused"])
            self.clear_pending()
            self.runtime.run_spend(NOW + 30)
            self.assertEqual([n["kind"] for n in self.runtime.queue.pending()], ["spend-cap"])
            self.clear_pending()
            self.runtime.run_spend(NOW + 60)
            self.assertEqual(self.runtime.queue.pending(), [])


if __name__ == "__main__":
    unittest.main()
