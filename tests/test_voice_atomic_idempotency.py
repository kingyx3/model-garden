"""Crash-consistent, single-call voice evidence regressions (#107, #230).

No tenant credentials, external provider, LiveKit or customer recordings.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("voice_atomic_evidence", ROOT / "platform/channels/call_transcript.py")
T = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = T
assert spec.loader is not None
spec.loader.exec_module(T)


class VoiceAtomicEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "voice" / "transcripts.jsonl"

    def record(self, call_hash="call-1", *, text="hello", agent="receptionist"):
        return {
            "type": "CallTranscript", "timestamp": 100, "endedAt": 101,
            "callHash": call_hash, "agent": agent,
            "turns": [{"speaker": "caller", "text": text}],
        }

    def write(self, record):
        T.store_private_jsonl(self.path, record, retention_seconds=1000, now=200)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_retry_of_identical_call_is_one_record_across_restarts(self):
        first = self.record()
        self.write(first)
        self.write(dict(first))
        self.assertEqual(self.rows(), [first])

    def test_conflicting_retry_cannot_replace_or_append_a_call(self):
        self.write(self.record())
        original = self.path.read_bytes()
        for different in (self.record(text="changed"), self.record(agent="another")):
            with self.assertRaisesRegex(ValueError, "conflicting call transcript"):
                self.write(different)
            self.assertEqual(self.path.read_bytes(), original)

    def test_distinct_calls_and_fallback_messages_remain_appendable(self):
        self.write(self.record("call-1"))
        self.write(self.record("call-2"))
        fallback = {"type": "FallbackMessage", "timestamp": 100, "message": "synthetic"}
        self.write(fallback)
        self.write(fallback)
        self.assertEqual(len(self.rows()), 4)

    def test_existing_duplicate_call_identity_fails_closed_without_data_loss(self):
        self.write(self.record())
        with self.path.open("a") as handle:
            handle.write(json.dumps(self.record()) + "\n")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "duplicate existing call transcript"):
            self.write(self.record("call-3"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_failure_before_atomic_replace_preserves_existing_history(self):
        self.write(self.record())
        original = self.path.read_bytes()
        with mock.patch.object(T.os, "replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                self.write(self.record("call-2"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.rows(), [self.record()])
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_symlinked_lock_cannot_redirect_or_modify_external_state(self):
        self.path.parent.mkdir(mode=0o700)
        outside = pathlib.Path(self.temp.name) / "other-tenant.lock"
        outside.write_text("keep")
        (self.path.parent / f".{self.path.name}.lock").symlink_to(outside)
        with self.assertRaises(OSError):
            self.write(self.record())
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse(self.path.exists())

    def test_concurrent_call_writers_do_not_lose_or_duplicate_records(self):
        calls = [self.record(f"call-{i}") for i in range(20)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(self.write, calls + calls))
        rows = self.rows()
        self.assertEqual(len(rows), 20)
        self.assertEqual({row["callHash"] for row in rows}, {f"call-{i}" for i in range(20)})


if __name__ == "__main__":
    unittest.main()
