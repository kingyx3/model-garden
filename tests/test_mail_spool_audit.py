"""Regression tests for durable Tier 0 mail intake audit acknowledgement (#77).

A receiving MTA must retry when the ledger is unavailable. A persisted message
must not reach the model before its acceptance is recorded; retries must repair
that gap without creating a second message or leaking the sender/body.
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


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spool_module = _load("mail_spool_audit_under_test", "platform/channels/mail_spool.py")
cli = _load("runtime_mailbox_audit_under_test", "scripts/runtime-mailbox.py")
ADDRESS = "support@client.example"
T0 = 1_800_000_000
PRIVATE = "private customer complaint with card number 4111 1111 1111 1111"


def message(body=PRIVATE):
    return (f"From: person@outside.example\r\nTo: {ADDRESS}\r\n"
            f"Message-ID: <audit-retry@outside.example>\r\nSubject: Sensitive\r\n\r\n{body}\r\n").encode()


class AuditRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name) / "mail"
        self.events = []

    def spool(self, audit):
        return spool_module.MailSpool(self.root, tenant="client-dev", addresses=[ADDRESS], audit=audit)

    def failed(self):
        def down(*_args):
            raise OSError("ledger down: " + PRIVATE)
        store = self.spool(down)
        with self.assertRaises(OSError):
            store.ingest(message(), now=T0)
        return store

    def test_a_persisted_unledgered_message_is_not_claimed_and_a_retry_repairs_it(self):
        first = self.failed()
        self.assertEqual(first.stats()["auditPending"], 1)
        self.assertEqual(first.claim(now=T0 + 1), [])
        self.assertEqual(first.stats()["new"], 1)
        self.assertNotIn(PRIVATE, first.index.read_text())

        recovered = self.spool(lambda *event: self.events.append(event))
        outcome = recovered.ingest(message(), now=T0 + 5)
        self.assertEqual(outcome["status"], "duplicate")
        self.assertEqual([event[0] for event in self.events], ["mail-spool.accepted"])
        self.assertEqual(recovered.stats()["auditPending"], 0)
        self.assertEqual(len(recovered.claim(now=T0 + 6)), 1)
        self.assertEqual(recovered.ingest(message(), now=T0 + 7)["status"], "duplicate")
        self.assertEqual(len(self.events), 1)

    def test_a_pending_receipt_cannot_be_cleared_by_a_different_payload_or_missing_audit_sink(self):
        self.failed()
        recovered = self.spool(lambda *event: self.events.append(event))
        with self.assertRaises(spool_module.SpoolError):
            recovered.ingest(message("substituted"), now=T0 + 1)
        with self.assertRaises(spool_module.SpoolError):
            self.spool(None).ingest(message(), now=T0 + 1)
        self.assertEqual(recovered.claim(now=T0 + 2), [])
        self.assertEqual(recovered.stats()["auditPending"], 1)
        self.assertEqual(self.events, [])

    def test_a_crash_after_ledger_append_retries_at_least_once_without_double_processing(self):
        calls = 0

        def ambiguous(*event):
            nonlocal calls
            calls += 1
            self.events.append(event)
            if calls == 1:
                raise OSError("process crashed after append")

        store = self.spool(ambiguous)
        with self.assertRaises(OSError):
            store.ingest(message(), now=T0)
        self.assertEqual(store.claim(now=T0 + 1), [])
        self.assertEqual(store.ingest(message(), now=T0 + 2)["status"], "duplicate")
        self.assertEqual(calls, 2)  # at-least-once ledger; downstream can dedupe by id
        self.assertEqual(len(store.claim(now=T0 + 3)), 1)
        self.assertEqual(store.claim(now=T0 + 4), [])

    def test_cli_returns_mta_tempfail_for_ledger_errors_and_retry_recovers(self):
        original = cli.GOVERN.record_operator_action
        def broken(*_args):
            raise ValueError("ledger corrupt: " + PRIVATE)
        cli.GOVERN.record_operator_action = broken
        self.addCleanup(setattr, cli.GOVERN, "record_operator_action", original)
        args = [str(self.root), "--tenant", "client-dev", "--address", ADDRESS, "ingest"]
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.main(args, stdin=io.BytesIO(message()))
        self.assertEqual(result, cli.TEMPFAIL)
        self.assertNotIn(PRIVATE, output.getvalue() + errors.getvalue())
        self.assertEqual(self.spool(None).stats()["auditPending"], 1)

        cli.GOVERN.record_operator_action = original
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(args, stdin=io.BytesIO(message()))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "duplicate")
        self.assertEqual(self.spool(None).stats()["auditPending"], 0)


if __name__ == "__main__":
    unittest.main()
