"""Fail-closed reference proof correlation tests (#117, #107).

These are credential-free collector tests, not live SIP or tenant acceptance.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reference_proof_under_test", ROOT / "scripts/reference-proof.py")
proof = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = proof
spec.loader.exec_module(proof)

ROOM = "a" * 64
OTHER = "b" * 64


def voice(kind, room=ROOM, timestamp=100, agent="receptionist"):
    return {"type": kind, "roomHash": room, "timestamp": timestamp, "agent": agent}


def evidence(voice_rows=(), audit_rows=()):
    return {
        "services": {"hermes": {"running": True}, "governed-tools": {"running": True},
                     "voice": {"running": True}},
        "voice": proof._voice_summary(list(voice_rows)),
        "governance": proof._audit_summary(list(audit_rows)),
    }


def check(data, *, call=True, fallback=False, action=False):
    return proof.evaluate(data, require_voice_call=call,
                          require_fallback=fallback, require_governed_action=action)


class ReferenceProofTests(unittest.TestCase):
    def test_orphan_start_is_not_a_completed_call(self):
        data = evidence([voice("CallStarted")])
        self.assertEqual(data["voice"]["distinctCalls"], 1)
        self.assertEqual(data["voice"]["completedCalls"], 0)
        self.assertFalse(check(data)["passed"])
        self.assertIn("no completed voice call", " ".join(check(data)["failures"]))

    def test_unrelated_end_or_fallback_does_not_complete_or_satisfy_handoff(self):
        rows = [voice("CallStarted"), voice("CallEnded", OTHER, 110),
                voice("HumanTransfer", OTHER, 105)]
        data = evidence(rows)
        self.assertEqual(data["voice"]["completedCalls"], 0)
        self.assertEqual(data["voice"]["completedCallFallbackEvidence"], 0)
        self.assertFalse(check(data, fallback=True)["passed"])

    def test_completed_call_requires_matching_agent_and_ordered_timestamps(self):
        for ending in (voice("CallEnded", agent="another", timestamp=110),
                       voice("CallEnded", timestamp=99),
                       voice("CallEnded", timestamp="not-a-time")):
            with self.subTest(ending=ending):
                self.assertEqual(evidence([voice("CallStarted"), ending])["voice"]["completedCalls"], 0)
        self.assertEqual(evidence([voice("CallStarted"), voice("CallEnded", timestamp=110)])["voice"]["completedCalls"], 1)

    def test_only_a_correlated_fallback_counts_and_duplicate_events_do_not_inflate(self):
        rows = [voice("CallStarted"), voice("CallStarted"),
                voice("CallEnded", timestamp=110), voice("CallEnded", timestamp=110),
                voice("HumanTransfer", OTHER, 105),
                voice("FallbackMessage", ROOM, 105)]
        data = evidence(rows)
        self.assertEqual(data["voice"]["completedCalls"], 1)
        self.assertEqual(data["voice"]["completedCallFallbackEvidence"], 1)
        self.assertTrue(check(data, fallback=True)["passed"])

    def test_transcript_write_failure_prevents_positive_voice_evidence(self):
        rows = [voice("CallStarted"), voice("CallEnded", timestamp=110),
                voice("TranscriptWriteFailed", timestamp=109)]
        data = evidence(rows)
        self.assertEqual(data["voice"]["transcriptWriteFailures"], 1)
        self.assertFalse(check(data)["passed"])
        self.assertIn("transcript", " ".join(check(data)["failures"]))

    def test_authorization_and_failed_execution_are_not_successful_business_actions(self):
        rows = [{"event": "authorization", "outcome": "allow", "tool": "calendar.book"},
                {"event": "execution", "outcome": "failed", "tool": "calendar.book"}]
        data = evidence([voice("CallStarted"), voice("CallEnded", timestamp=110)], rows)
        self.assertEqual(data["governance"]["records"], 2)
        self.assertEqual(data["governance"]["successfulActions"], 0)
        self.assertFalse(check(data, action=True)["passed"])
        data = evidence([voice("CallStarted"), voice("CallEnded", timestamp=110)],
                        rows + [{"event": "execution", "outcome": "succeeded", "tool": "calendar.book"}])
        self.assertEqual(data["governance"]["successfulActions"], 1)
        self.assertTrue(check(data, action=True)["passed"])


if __name__ == "__main__":
    unittest.main()
