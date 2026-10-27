"""Adversarial privacy and replay checks for the post-call summary cache (#107)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("call_summary_cache_security_under_test", ROOT / "platform/channels/call_summary.py")
summary = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = summary
spec.loader.exec_module(summary)


class FakeClient:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return {"text": self.answer, "usage": {}}


def call(text: str = "Please send a plumber.") -> dict:
    return {
        "callHash": "a" * 64, "agent": "receptionist", "timestamp": 1790000000,
        "endedAt": 1790000030, "outcome": "message-taken",
        "turns": [{"speaker": "caller", "index": 0, "offsetSeconds": 0, "text": text}],
    }


class PrivateCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.cache = self.root / "private" / "call-summaries.json"

    def run_summary(self, answer="Fresh answer.", record=None):
        client = FakeClient(answer)
        result = summary.CallSummarizer(client, cache_path=self.cache)(record or call())
        return result, client

    def test_private_regular_cache_is_reused_without_another_model_call(self):
        first, _ = self.run_summary("Original answer.")
        again, client = self.run_summary("Changed answer.")
        self.assertEqual((first["summary"], again["summary"]), ("Original answer.", "Original answer."))
        self.assertEqual(client.calls, 0)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.cache.parent.stat().st_mode), 0o700)

    def test_symlinked_cache_does_not_read_or_clobber_the_target(self):
        self.cache.parent.mkdir(mode=0o700)
        victim = self.root / "victim.json"
        victim.write_text('{"private":"do not overwrite"}', encoding="utf-8")
        self.cache.symlink_to(victim)
        result, client = self.run_summary()
        self.assertEqual((result["summary"], client.calls), ("Fresh answer.", 1))
        self.assertEqual(victim.read_text(encoding="utf-8"), '{"private":"do not overwrite"}')
        self.assertTrue(self.cache.is_symlink())

    def test_hardlinked_cache_does_not_read_or_clobber_the_other_name(self):
        self.cache.parent.mkdir(mode=0o700)
        victim = self.root / "victim.json"
        victim.write_text('{"private":"do not overwrite"}', encoding="utf-8")
        os.chmod(victim, 0o600)
        os.link(victim, self.cache)
        result, client = self.run_summary()
        self.assertEqual((result["summary"], client.calls), ("Fresh answer.", 1))
        self.assertEqual(victim.read_text(encoding="utf-8"), '{"private":"do not overwrite"}')
        self.assertEqual(os.stat(victim).st_nlink, 2)

    def test_world_readable_cache_is_not_trusted_or_rewritten(self):
        self.run_summary("Original answer.")
        original = self.cache.read_bytes()
        os.chmod(self.cache, 0o644)
        result, client = self.run_summary("New answer.")
        self.assertEqual((result["summary"], client.calls), ("New answer.", 1))
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o644)

    def test_symlinked_parent_is_not_used_for_private_cache(self):
        target = self.root / "other"
        target.mkdir(mode=0o700)
        self.cache.parent.symlink_to(target, target_is_directory=True)
        result, client = self.run_summary()
        self.assertEqual((result["summary"], client.calls), ("Fresh answer.", 1))
        self.assertFalse((target / self.cache.name).exists())

    def test_same_call_id_with_changed_turns_never_reuses_old_approval_wording(self):
        self.run_summary("Original answer.", call("I need a plumber."))
        result, client = self.run_summary("Different request.", call("I need to cancel the visit."))
        self.assertEqual((result["summary"], client.calls), ("Different request.", 1))
        again, cached_client = self.run_summary("Should not be called.", call("I need to cancel the visit."))
        self.assertEqual((again["summary"], cached_client.calls), ("Different request.", 0))

    def test_failed_atomic_replace_preserves_previous_cache_and_leaves_no_temp_file(self):
        self.run_summary("Original answer.")
        original = self.cache.read_bytes()
        with mock.patch.object(summary.os, "replace", side_effect=OSError("simulated failure")):
            result, _ = self.run_summary("Changed answer.", call("Different turn"))
        self.assertEqual(result["summary"], "Changed answer.")
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.cache.parent.iterdir()), [self.cache.name])

    def test_oversized_cache_is_not_read_or_rewritten(self):
        self.cache.parent.mkdir(mode=0o700)
        self.cache.write_bytes(b" " * (summary.MAX_CACHE_BYTES + 1))
        os.chmod(self.cache, 0o600)
        result, client = self.run_summary()
        self.assertEqual((result["summary"], client.calls), ("Fresh answer.", 1))
        self.assertEqual(self.cache.stat().st_size, summary.MAX_CACHE_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
