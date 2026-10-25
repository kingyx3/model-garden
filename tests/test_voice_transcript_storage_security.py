"""Fail-closed privacy tests for voice transcript persistence (#107).

Synthetic data only; no LiveKit, credentials or client recordings.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("voice_transcript_storage_security", ROOT / "platform/channels/call_transcript.py")
T = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = T
assert spec.loader is not None
spec.loader.exec_module(T)


class PrivateTranscriptStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.path = self.root / "voice" / "transcripts.jsonl"
        self.config = T.TranscriptConfig(path=self.path)

    def transcript(self):
        call = T.CallTranscript("synthetic-call", "receptionist", self.config)
        call.add_turn("caller", "Synthetic private request")
        return call

    def assert_rejected(self):
        errors = []
        with self.assertLogs("model-garden.call-transcript", level="ERROR") as logs:
            self.assertIsNone(T.finalize(self.transcript(), on_failure=errors.append))
        self.assertEqual(len(errors), 1)
        self.assertNotIn("Synthetic private request", " ".join(logs.output + errors))

    def test_existing_symlink_cannot_redirect_transcript_to_another_file(self):
        self.path.parent.mkdir()
        target = self.root / "other-client.jsonl"
        target.write_text("unchanged\n")
        self.path.symlink_to(target)
        self.assert_rejected()
        self.assertEqual(target.read_text(), "unchanged\n")

    def test_hardlinked_file_is_rejected_without_chmod_or_truncating_the_other_link(self):
        self.path.parent.mkdir()
        target = self.root / "other-client.jsonl"
        target.write_text("unchanged\n")
        before_mode = stat.S_IMODE(target.stat().st_mode)
        os.link(target, self.path)
        self.assert_rejected()
        self.assertEqual(target.read_text(), "unchanged\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), before_mode)

    def test_parent_symlink_is_rejected_without_touching_the_target_directory(self):
        outside = self.root / "other-client"
        outside.mkdir(mode=0o755)
        self.path.parent.symlink_to(outside, target_is_directory=True)
        self.assert_rejected()
        self.assertFalse((outside / self.path.name).exists())
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    def test_nonregular_fifo_is_rejected_without_writing_or_hanging(self):
        self.path.parent.mkdir()
        os.mkfifo(self.path)
        self.assert_rejected()
        self.assertTrue(stat.S_ISFIFO(self.path.stat().st_mode))

    def test_replacement_after_lock_acquisition_is_rejected_before_truncate(self):
        self.path.parent.mkdir()
        self.path.write_text("original\n")
        target = self.root / "other-client.jsonl"
        target.write_text("unchanged\n")
        original_flock = T.fcntl.flock
        replaced = False

        def replace_on_lock(fd, operation):
            nonlocal replaced
            result = original_flock(fd, operation)
            if operation == T.fcntl.LOCK_EX and not replaced:
                replaced = True
                self.path.unlink()
                self.path.symlink_to(target)
            return result

        with mock.patch.object(T.fcntl, "flock", side_effect=replace_on_lock):
            self.assert_rejected()
        self.assertTrue(replaced)
        self.assertEqual(target.read_text(), "unchanged\n")

    def test_directory_replacement_while_waiting_for_lock_fails_closed(self):
        self.path.parent.mkdir()
        self.path.write_text("original\n")
        original_flock = T.fcntl.flock
        replaced = False

        def replace_directory(fd, operation):
            nonlocal replaced
            result = original_flock(fd, operation)
            if operation == T.fcntl.LOCK_EX and not replaced:
                replaced = True
                self.path.parent.rename(self.root / "detached")
                self.path.parent.mkdir()
            return result

        with mock.patch.object(T.fcntl, "flock", side_effect=replace_directory):
            self.assert_rejected()
        self.assertTrue(replaced)
        self.assertEqual((self.root / "detached" / self.path.name).read_text(), "original\n")
        self.assertFalse(self.path.exists())

    def test_owned_existing_regular_file_still_supports_retention_and_private_mode(self):
        self.path.parent.mkdir()
        self.path.write_text('{"timestamp":1,"callHash":"expired"}\n')
        self.assertIsNotNone(T.finalize(self.transcript()))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertIn("synthetic-call", self.path.read_text())
        self.assertNotIn("expired", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
