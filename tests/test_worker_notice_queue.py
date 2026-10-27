"""Regression tests for durable worker notices when the queue reaches capacity."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker_notice_queue_under_test", ROOT / "platform/runtime/worker.py")
worker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)


class NoticeQueueCapacityTests(unittest.TestCase):
    def test_full_queue_rejects_new_handoff_but_keeps_existing_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            original = worker.MAX_QUEUE
            worker.MAX_QUEUE = 1
            try:
                queue = worker.NoticeQueue(pathlib.Path(directory))
                first = queue.add("mail-needs-person", "owner@client.example", "first", reference="thread-1")
                with self.assertRaisesRegex(RuntimeError, "notice queue full"):
                    queue.add("mail-needs-person", "owner@client.example", "second", reference="thread-2")
                self.assertEqual(queue.add("mail-needs-person", "owner@client.example", "first", reference="thread-1"), first)
                self.assertEqual(len(queue.pending()), 1)
                self.assertEqual(queue.pending()[0]["reference"], "thread-1")
            finally:
                worker.MAX_QUEUE = original


if __name__ == "__main__":
    unittest.main()
