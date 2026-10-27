"""Regression: local inbox notices survive delivery until a host operator explicitly acknowledges them.

This is host-local operational acknowledgement, not authenticated client-owner or provider delivery proof.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = load("worker_inbox_notice_test", "platform/runtime/worker.py")
inbox = load("inbox_notice_test", "platform/core/inbox.py")
cli = load("inbox_notice_cli_test", "scripts/runtime-inbox.py")
delivery = worker._module("delivery")
OWNER = "owner@client.example"
OTHER = "other@client.example"
SECRET = "4111 1111 1111 1111 customer address"


class LocalInboxNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.queue = worker.NoticeQueue(self.root / "worker")
        self.directory = delivery.Directory({OWNER: {"channel": "inbox"}, OTHER: {"channel": "inbox"}})

    def post(self, to=OWNER):
        key = self.queue.add("mail-needs-person", to, SECRET, reference="thread-ref")
        results = delivery.deliver([self.queue.source()], self.directory, {})
        self.assertEqual(results[0]["status"], "delivered")
        return key

    def snapshot(self):
        return inbox.collect(approvals=self.root / "approvals", handoffs=self.root / "handoffs",
                             notices=self.root / "worker", now=1790000000)

    def test_inbox_delivery_keeps_notice_durable_visible_and_private_until_ack(self):
        key = self.post()
        self.assertEqual(len(self.queue.pending()), 1)
        self.assertEqual(delivery.deliver([self.queue.source()], self.directory, {}), [])
        snapshot = self.snapshot()
        (item,) = [item for item in snapshot["items"] if item["kind"] == "owner-notice"]
        self.assertEqual((item["key"], item["to"], item["noticeKind"]), (key, OWNER, "mail-needs-person"))
        rendered = json.dumps(snapshot) + inbox.format_text(snapshot) + inbox.format_chat(snapshot)
        self.assertNotIn(SECRET, rendered)
        self.assertIn("NOTICE", inbox.format_text(snapshot))
        self.assertEqual(len(inbox.for_person(snapshot, OWNER, everything=False)["items"]), 1)
        self.assertEqual(inbox.for_person(snapshot, OTHER, everything=False)["items"], [])
        self.assertEqual(len(worker.NoticeQueue(self.root / "worker").pending()), 1)

    def test_ack_requires_posted_inbox_notice_and_a_successful_audit(self):
        key = self.queue.add("mail-needs-person", OWNER, SECRET, reference="thread-ref")
        events = []
        with self.assertRaisesRegex(ValueError, "no such inbox notice"):
            self.queue.ack_inbox(key, actor="host-operator", audit=lambda *a: events.append(a))
        self.assertEqual(len(self.queue.pending()), 1)
        self.post()
        with self.assertRaisesRegex(ValueError, "no such inbox notice"):
            self.queue.ack_inbox("0" * 24, actor="host-operator", audit=lambda *a: events.append(a))
        with self.assertRaisesRegex(ValueError, "no such inbox notice"):
            self.queue.ack_inbox("../bad", actor="host-operator", audit=lambda *a: events.append(a))
        with self.assertRaises(RuntimeError):
            self.queue.ack_inbox(key, actor="host-operator", audit=lambda *a: (_ for _ in ()).throw(RuntimeError("ledger down")))
        self.assertEqual(len(self.queue.pending()), 1)
        self.queue.ack_inbox(key, actor="host-operator", audit=lambda *a: events.append(a))
        self.assertEqual(self.queue.pending(), [])
        self.assertEqual(events[0][0], "notice.operator-ack")
        self.assertNotIn(SECRET, json.dumps(events))

    def test_cli_ack_is_host_operator_scoped_and_audited(self):
        key = self.post()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--data-root", str(self.root), "--ack-notice", key]), 0)
        self.assertIn(key, out.getvalue())
        self.assertEqual(self.snapshot()["items"], [])
        audit = (self.root / "audit.jsonl").read_text()
        self.assertIn("notice.operator-ack", audit)
        self.assertIn("host-operator:", audit)
        self.assertNotIn(SECRET, audit)

    def test_external_delivery_still_removes_queue_notice_and_is_not_in_inbox(self):
        queue = worker.NoticeQueue(self.root / "worker")
        queue.add("mail-needs-person", OWNER, "safe", reference="other")
        sent = []
        external = delivery.Directory({OWNER: {"channel": "slack", "address": "D0123"}})
        results = delivery.deliver([queue.source()], external, {"slack": lambda address, message: sent.append((address, message))})
        self.assertEqual(results[0]["status"], "delivered")
        self.assertEqual(len(sent), 1)
        self.assertEqual(queue.pending(), [])
        self.assertEqual(self.snapshot()["items"], [])

    def test_corrupt_notice_state_fails_closed_not_as_empty_inbox(self):
        self.queue.root.mkdir(parents=True, exist_ok=True)
        self.queue.path.write_text('{"pending": {"x": {"inboxPosted": true, "text": "unsafe"}}}')
        with self.assertRaises(ValueError):
            self.snapshot()


if __name__ == "__main__":
    unittest.main()
