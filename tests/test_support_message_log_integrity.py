"""Adversarial durability tests for the built-in customer mailbox's handled-message witness.

An absent history and absent initialized lock means a fresh mailbox. After the
first handled message, a missing, corrupt, linked or exposed history must *never*
be treated as empty: that could redraft or redeliver customer mail.
All messages, addresses and paths here are synthetic.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "support_mailbox_dedupe_integrity", ROOT / "platform/channels/support_mailbox.py"
)
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
assert spec.loader is not None
spec.loader.exec_module(support)


class HandledMessageIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.root = self.base / "handled"
        self.log = support.MessageLog(self.root)

    def test_new_and_restarted_witness_is_private_and_idempotent(self):
        self.assertFalse(self.log.seen("<synthetic-1@example.invalid>"))
        self.log.mark("<synthetic-1@example.invalid>")
        self.assertTrue(support.MessageLog(self.root).seen("<synthetic-1@example.invalid>"))
        self.log.mark("<synthetic-1@example.invalid>")
        self.assertEqual(len(__import__("json").loads(self.log.path.read_text())), 1)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.log.path.stat().st_mode), 0o600)

    def test_malformed_or_truncated_existing_witness_fails_closed(self):
        self.log.mark("<synthetic-1@example.invalid>")
        for payload in ("{", "[] trailing", '["not-a-sha"]', '{"unexpected":"shape"}'):
            self.log.path.write_text(payload)
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.log.seen("<synthetic-1@example.invalid>")
                with self.assertRaises(ValueError):
                    self.log.mark("<synthetic-2@example.invalid>")

    def test_linked_witness_never_reads_or_overwrites_an_outside_file(self):
        self.log.mark("<synthetic-1@example.invalid>")
        outside = self.base / "outside"
        outside.write_text("keep outside private")
        self.log.path.unlink()
        self.log.path.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            self.log.seen("<synthetic-1@example.invalid>")
        with self.assertRaises((OSError, ValueError)):
            self.log.mark("<synthetic-2@example.invalid>")
        self.assertEqual(outside.read_text(), "keep outside private")
        self.log.path.unlink()
        os.link(outside, self.log.path)
        with self.assertRaises((OSError, ValueError)):
            self.log.seen("<synthetic-1@example.invalid>")

    def test_exposed_witness_and_directory_fail_closed(self):
        self.log.mark("<synthetic-1@example.invalid>")
        os.chmod(self.log.path, 0o644)
        with self.assertRaises(ValueError):
            self.log.seen("<synthetic-1@example.invalid>")
        os.chmod(self.log.path, 0o600)
        os.chmod(self.root, 0o755)
        with self.assertRaises(ValueError):
            self.log.mark("<synthetic-2@example.invalid>")

    def test_linked_lock_cannot_redirect_a_write(self):
        self.log.mark("<synthetic-1@example.invalid>")
        outside = self.base / "outside"
        outside.write_text("unchanged")
        lock = self.root / ".support-mailbox.lock"
        lock.unlink()
        lock.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            self.log.mark("<synthetic-2@example.invalid>")
        self.assertEqual(outside.read_text(), "unchanged")

    def test_predictable_temporary_hardlink_cannot_clobber_outside_file(self):
        self.log.mark("<synthetic-1@example.invalid>")
        outside = self.base / "outside"
        outside.write_text("do not truncate")
        os.link(outside, self.log.path.with_suffix(".tmp"))
        self.log.mark("<synthetic-2@example.invalid>")
        self.assertEqual(outside.read_text(), "do not truncate")
        self.assertTrue(self.log.seen("<synthetic-2@example.invalid>"))

    def test_corrupt_witness_blocks_customer_draft_before_any_effect(self):
        self.log.mark("<synthetic-1@example.invalid>")
        self.log.path.write_text("corrupt")
        email = support._email()
        drafts = []
        adapter = email.EmailChannelAdapter(
            "mx.client.example", own_addresses=("support@client.example",),
            reply_limiter=email.ReplyLimiter(self.base / "limits", 2),
        )
        mailbox = support.SupportMailbox(
            adapter, email.TEXT.PrincipalDirectory(),
            {"mail.draft": lambda args: drafts.append(args) or {"draftId": "d-1"}},
            classify=lambda message: "enquiry",
            answer=lambda message, category: {"text": "Open weekdays.", "sources": ["kb:hours"]},
            handled=self.log,
        )
        message = (
            b"Authentication-Results: mx.client.example; dmarc=pass header.from=example.invalid\r\n"
            b"From: customer@example.invalid\r\nTo: support@client.example\r\n"
            b"Message-ID: <synthetic-2@example.invalid>\r\n"
            b"Subject: Hours\r\nContent-Type: text/plain\r\n\r\nWhen are you open?\r\n"
        )
        with self.assertRaises(ValueError):
            mailbox.handle(message, now=1800000000)
        self.assertEqual(drafts, [])


if __name__ == "__main__":
    unittest.main()
