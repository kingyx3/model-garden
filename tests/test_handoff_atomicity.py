"""Concurrency regression checks for the file-backed Tier 4 handoff lifecycle.

Mutations come from independent runtime, notification and reply workers. The POSIX lock
protects a whole read/modify/write transaction, not only the final os.replace.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("handoff_atomic_under_test", ROOT / "platform/core/handoff.py")
handoff = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = handoff
spec.loader.exec_module(handoff)


def request(num: int = 1) -> dict:
    return {"requestId": f"{num:064x}", "tool": "calendar.book", "arguments": {"start": "tomorrow"}}


class HandoffAtomicityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name) / "handoffs"

    def create(self, num=1, **kwargs):
        return handoff.create(
            self.root, request(num), capability="calendar", reason="unbound",
            assignee="jordan@client.example", now=1000, **kwargs,
        )

    def competing(self, one, two):
        barrier = threading.Barrier(3)

        def start(operation):
            barrier.wait(timeout=5)
            try:
                return ("ok", operation())
            except Exception as exc:
                return ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(start, one)
            b = pool.submit(start, two)
            barrier.wait(timeout=5)
            return [a.result(timeout=5), b.result(timeout=5)]

    def test_acknowledgement_and_notice_receipt_cannot_overwrite_each_other(self):
        doc = self.create()
        original = handoff._write

        def slow_write(*args, **kwargs):
            # A fixed sleep after both threads enter the race triggers the old lost-update bug.
            time.sleep(0.04)
            return original(*args, **kwargs)

        with mock.patch.object(handoff, "_write", side_effect=slow_write):
            results = self.competing(
                lambda: handoff.acknowledge(self.root, doc["id"], "jordan@client.example", via="inbox", now=1001),
                lambda: handoff.mark_notified(self.root, doc["id"], "assigned", via="inbox", now=1002),
            )
        self.assertEqual([status for status, _ in results], ["ok", "ok"])
        updated = handoff.find(self.root, doc["id"])
        self.assertEqual(updated["acknowledged"]["by"], "jordan@client.example")
        self.assertEqual(updated["notices"]["assigned"]["via"], "inbox")

    def test_two_simultaneous_completions_only_one_succeeds(self):
        doc = self.create()
        original = handoff._write

        def slow_write(*args, **kwargs):
            time.sleep(0.04)
            return original(*args, **kwargs)

        with mock.patch.object(handoff, "_write", side_effect=slow_write):
            results = self.competing(
                lambda: handoff.complete(self.root, doc["id"], "done", "jordan", now=1200),
                lambda: handoff.complete(self.root, doc["id"], "declined", "jordan", now=1200),
            )
        self.assertEqual(sorted(status for status, _ in results), ["error", "ok"])
        failure = next(value for status, value in results if status == "error")
        self.assertIsInstance(failure, handoff.HandoffError)
        self.assertEqual(len(handoff.closed(self.root)), 1)
        self.assertEqual(handoff.pending(self.root), [])

    def test_same_request_concurrently_created_once_without_deadline_reset(self):
        original = handoff._write

        def slow_write(*args, **kwargs):
            time.sleep(0.04)
            return original(*args, **kwargs)

        with mock.patch.object(handoff, "_write", side_effect=slow_write):
            results = self.competing(
                lambda: self.create(),
                lambda: handoff.create(
                    self.root, request(), capability="calendar", reason="unbound",
                    assignee="jordan@client.example", now=2000,
                ),
            )
        self.assertEqual([status for status, _ in results], ["ok", "ok"])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(len(handoff.pending(self.root)), 1)

    def test_reopened_and_completed_in_same_second_preserves_both_receipts(self):
        self.create()
        handoff.complete(self.root, request()["requestId"], "done", "jordan", now=1100)
        self.create()
        handoff.complete(self.root, request()["requestId"], "declined", "jordan", now=1100)
        self.assertEqual(sorted(entry["status"] for entry in handoff.closed(self.root)), ["declined", "done"])

    def test_file_permissions_and_no_shared_temporary_file(self):
        doc = self.create()
        handoff.mark_notified(self.root, doc["id"], "assigned", via="inbox")
        handoff.acknowledge(self.root, doc["id"], "jordan", via="inbox")
        handoff.complete(self.root, doc["id"], "done", "jordan")
        self.assertEqual(stat.S_IMODE(os.stat(self.root / ".lock").st_mode), 0o600)
        self.assertFalse(list(self.root.rglob("*.tmp")))
        for path in self.root.rglob("*.json"):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
