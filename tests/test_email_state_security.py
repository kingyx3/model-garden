"""Adversarial filesystem regressions for email reply quotas and approval receipts (#84/#109).

Synthetic local files only. A corrupt or redirected state must never authorize an
extra customer reply or reuse an exact-action approval link.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile
import unittest

import importlib.util

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("email_state_security", ROOT / "platform" / "channels" / "email_channel.py")
mail = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mail)

SECRET = b"s" * 32
REQUEST = "a" * 64
APPROVER = "owner@client.example"


class ReplyQuotaSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "limits"
        self.root.mkdir(mode=0o700)
        self.thread = "<synthetic-thread@client.example>"
        self.state = self.root / hashlib.sha256(self.thread.encode()).hexdigest()[:32]

    def test_symlinked_counter_never_truncates_unrelated_file(self):
        victim = self.base / "unrelated"
        victim.write_text("preserve this file")
        self.state.symlink_to(victim)
        with self.assertRaises(OSError):
            mail.ReplyLimiter(self.root, limit=2).consume(self.thread)
        self.assertEqual(victim.read_text(), "preserve this file")

    def test_hardlinked_and_exposed_counters_are_refused(self):
        for attack in ("hardlink", "permissive"):
            with self.subTest(attack=attack):
                self.state.write_text("1")
                self.state.chmod(0o600)
                other = self.base / "other"
                if attack == "hardlink":
                    os.link(self.state, other)
                else:
                    self.state.chmod(0o644)
                with self.assertRaises(OSError):
                    mail.ReplyLimiter(self.root, limit=2).consume(self.thread)
                self.assertEqual(self.state.read_text(), "1")
                if other.exists():
                    other.unlink()
                self.state.unlink()

    def test_corrupt_or_oversized_counter_cannot_reset_quota(self):
        for bad in ("not-a-count", "1\n2", "99999999999999999"):
            with self.subTest(bad=bad):
                self.state.write_text(bad)
                self.state.chmod(0o600)
                with self.assertRaises(OSError):
                    mail.ReplyLimiter(self.root, limit=2).consume(self.thread)
                self.assertEqual(self.state.read_text(), bad)

    def test_symlinked_or_exposed_counter_directory_is_refused(self):
        root_link = self.base / "redirect"
        root_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            mail.ReplyLimiter(root_link).consume(self.thread)
        self.root.chmod(0o755)
        with self.assertRaises(OSError):
            mail.ReplyLimiter(self.root).consume(self.thread)

    def test_corrupt_state_suppresses_automatic_reply(self):
        self.state.write_text("bad")
        self.state.chmod(0o600)
        adapter = mail.EmailChannelAdapter(
            "mx.client.example", reply_limiter=mail.ReplyLimiter(self.root, limit=2)
        )
        inbound = (
            "Authentication-Results: mx.client.example; dmarc=pass header.from=client.example\r\n"
            "From: customer@client.example\r\nTo: agent@client.example\r\n"
            "Message-ID: <synthetic-thread@client.example>\r\n"
            "Content-Type: text/plain\r\n\r\nHello"
        ).encode()
        self.assertEqual(adapter.should_reply(inbound), (False, "thread-reply-state-unavailable"))
        self.assertEqual(self.state.read_text(), "bad")

    def test_valid_quota_still_survives_restart(self):
        self.assertTrue(mail.ReplyLimiter(self.root, limit=2).consume(self.thread))
        self.assertTrue(mail.ReplyLimiter(self.root, limit=2).consume(self.thread))
        self.assertFalse(mail.ReplyLimiter(self.root, limit=2).consume(self.thread))
        self.assertEqual(self.state.read_text(), "2")


class ApprovalStateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "spent"
        self.links = mail.ApprovalLinks(SECRET, self.root, clock=lambda: 1_800_000_000)
        self.token = self.links.issue(REQUEST, APPROVER, "approve")

    def test_symlinked_spent_directory_cannot_redirect_approval(self):
        other = self.base / "other-tenant"
        other.mkdir(mode=0o700)
        self.root.symlink_to(other, target_is_directory=True)
        with self.assertRaises(mail.ChannelVerificationError):
            self.links.peek(self.token)
        with self.assertRaises(mail.ChannelVerificationError):
            self.links.consume(self.token)
        self.assertEqual(list(other.iterdir()), [])

    def test_exposed_spent_directory_refuses_both_preview_and_commit(self):
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o755)
        with self.assertRaises(mail.ChannelVerificationError):
            self.links.peek(self.token)
        with self.assertRaises(mail.ChannelVerificationError):
            self.links.consume(self.token)

    def test_dangling_spent_symlink_is_not_an_unspent_approval(self):
        self.root.mkdir(mode=0o700)
        marker = self.root / self.links._pair(REQUEST, APPROVER)
        marker.symlink_to(self.base / "missing")
        with self.assertRaisesRegex(mail.ChannelVerificationError, "already used"):
            self.links.peek(self.token)
        with self.assertRaisesRegex(mail.ChannelVerificationError, "already used"):
            self.links.consume(self.token)

    def test_approval_receipt_is_durable_and_single_use_across_restart(self):
        self.assertEqual(self.links.peek(self.token)[0], REQUEST)
        self.assertEqual(self.links.consume(self.token).decision, "approve")
        marker = self.root / self.links._pair(REQUEST, APPROVER)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        other = mail.ApprovalLinks(SECRET, self.root, clock=lambda: 1_800_000_000)
        with self.assertRaisesRegex(mail.ChannelVerificationError, "already used"):
            other.peek(self.token)
        with self.assertRaisesRegex(mail.ChannelVerificationError, "already used"):
            other.consume(self.token)


if __name__ == "__main__":
    unittest.main()
