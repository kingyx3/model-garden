"""Fail-closed private voice evidence path regressions (#107).

These tests use synthetic records and no LiveKit, SIP, customer data or credentials.
The writer must never silently repair attacker-controlled permissions or traverse
a symlinked ancestor before publishing call/transcript/fallback evidence.
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
SOURCE = ROOT / "platform/channels/call_transcript.py"
spec = importlib.util.spec_from_file_location("voice_private_path_under_test", SOURCE)
T = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = T
assert spec.loader is not None
spec.loader.exec_module(T)


class PrivateVoicePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.path = self.private / "records.jsonl"

    def write(self, path=None, *, timestamp=100):
        T.store_private_jsonl(
            self.path if path is None else path,
            {"type": "FallbackMessage", "timestamp": timestamp, "message": "synthetic"},
            retention_seconds=3600, now=timestamp,
        )

    def test_new_private_directory_and_file_are_created_without_symlink_traversal(self):
        path = self.private / "new" / "nested" / "records.jsonl"
        self.write(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.parent.parent.stat().st_mode), 0o700)

    def test_symlinked_intermediate_directory_cannot_redirect_new_voice_records(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        (self.private / "alias").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.write(self.private / "alias" / "nested" / "records.jsonl")
        self.assertFalse((outside / "nested").exists())

    def test_existing_public_directory_is_refused_not_repaired(self):
        exposed = self.root / "exposed"
        exposed.mkdir(mode=0o755)
        with self.assertRaises(OSError):
            self.write(exposed / "records.jsonl")
        self.assertEqual(stat.S_IMODE(exposed.stat().st_mode), 0o755)
        self.assertFalse((exposed / "records.jsonl").exists())

    def test_existing_permissive_lock_is_refused_not_chmodded(self):
        self.write()
        lock = self.private / f".{self.path.name}.lock"
        original = self.path.read_bytes()
        lock.chmod(0o666)
        with self.assertRaises(OSError):
            self.write(timestamp=101)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o666)
        self.assertEqual(self.path.read_bytes(), original)

    def test_existing_permissive_payload_is_refused_not_chmodded(self):
        self.write()
        original = self.path.read_bytes()
        self.path.chmod(0o644)
        with self.assertRaises(OSError):
            self.write(timestamp=101)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual(self.path.read_bytes(), original)

    def test_relative_and_dotdot_paths_are_refused(self):
        for path in (pathlib.Path("relative/records.jsonl"), self.private / ".." / "private" / "records.jsonl"):
            with self.subTest(path=path), self.assertRaises(OSError):
                self.write(path)


if __name__ == "__main__":
    unittest.main()
