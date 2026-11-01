"""Credential-free full-call transcript acceptance regressions for #107.

This is structural local evidence, not a SIP/media, legal-consent or provider attestation.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reference_proof_full_coverage", ROOT / "scripts/reference-proof.py")
proof = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = proof
SPEC.loader.exec_module(proof)

AGENT = "model-garden-receptionist"
A, B = "a" * 64, "b" * 64


def lifecycle(call_hash: str, at: int = 100, *, ended: bool = True):
    rows = [{"type": "CallStarted", "callHash": call_hash, "agent": AGENT, "timestamp": at}]
    if ended:
        rows.append({"type": "CallEnded", "callHash": call_hash, "agent": AGENT, "timestamp": at + 10})
    return rows


def transcript(call_hash: str, at: int = 100, **extra):
    record = {
        "type": "CallTranscript", "callHash": call_hash, "agent": AGENT,
        "timestamp": at, "endedAt": at + 10, "outcome": "resolved",
        "summary": "The caller asked a question.", "summarySource": "extractive",
        "disclosure": {"configured": True, "played": True}, "droppedTurns": 0,
        "turns": [
            {"index": 0, "speaker": "caller", "offsetSeconds": 1, "text": "Hello"},
            {"index": 1, "speaker": "agent", "offsetSeconds": 2, "text": "How can I help?"},
        ],
    }
    return {**record, **extra}


def evidence(rows, records):
    return {
        "services": {"hermes": {"running": True}, "voice": {"running": True}},
        "voice": proof._voice_summary(rows),
        "transcripts": proof._transcript_summary(rows, records),
        "governance": {"successfulActions": 0, "completedCallActions": 0},
    }


def acceptance(rows, records):
    return proof.evaluate(
        evidence(rows, records), require_voice_call=False,
        require_fallback=False, require_governed_action=False,
        require_full_call_coverage=True,
    )


class FullCallCoverageTests(unittest.TestCase):
    def test_all_completed_calls_need_their_own_complete_two_way_transcript(self):
        rows = lifecycle(A) + lifecycle(B, 200)
        self.assertFalse(acceptance(rows, [transcript(A)])["passed"])
        self.assertTrue(acceptance(rows, [transcript(A), transcript(B, 200)])["passed"])

    def test_incomplete_or_duplicated_lifecycle_and_transcripts_cannot_pass(self):
        for rows, records in (
            (lifecycle(A) + lifecycle(B, 200, ended=False), [transcript(A)]),
            (lifecycle(A), [transcript(A), transcript(A)]),
            (lifecycle(A), [transcript(A, disclosure={"configured": True, "played": False})]),
            (lifecycle(A), [transcript(A, droppedTurns=1)]),
        ):
            with self.subTest(rows=rows, records=records):
                self.assertFalse(acceptance(rows, records)["passed"])

    def test_cold_transfer_is_two_way_but_not_complete_through_human_leg(self):
        rows = lifecycle(A)
        record = transcript(A, outcome="transferred", transfer={
            "offsetSeconds": 4, "turnsAfter": 0, "captured": False,
        })
        summary = proof._transcript_summary(rows, [record])
        self.assertEqual(summary["completeTwoWayCalls"], 1)
        self.assertEqual(summary["completeThroughTransferCalls"], 0)
        self.assertFalse(acceptance(rows, [record])["passed"])

    def test_forged_transfer_capture_without_a_human_turn_cannot_pass(self):
        rows = lifecycle(A)
        base = transcript(A, outcome="transferred")
        for changed in (
            {"transfer": {"offsetSeconds": 4, "turnsAfter": 1, "captured": True}},
            {"transfer": {"offsetSeconds": 4, "turnsAfter": 1, "captured": True},
             "turns": base["turns"] + [{"index": 2, "speaker": "agent", "offsetSeconds": 5, "text": "Done", "afterTransfer": True}]},
            {"transfer": {"offsetSeconds": 4, "turnsAfter": 1, "captured": True},
             "turns": base["turns"] + [{"index": 2, "speaker": "human", "offsetSeconds": 3, "text": "Hello", "afterTransfer": True}]},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(acceptance(rows, [{**base, **changed}])["passed"])

    def test_valid_human_turn_after_transfer_passes_structural_gate_only(self):
        rows = lifecycle(A)
        record = transcript(A, outcome="transferred",
                            transfer={"offsetSeconds": 4, "turnsAfter": 1, "captured": True},
                            turns=transcript(A)["turns"] + [
                                {"index": 2, "speaker": "human", "offsetSeconds": 5, "text": "I can help", "afterTransfer": True},
                            ])
        summary = proof._transcript_summary(rows, [record])
        self.assertEqual(summary["completeThroughTransferCalls"], 1)
        self.assertTrue(acceptance(rows, [record])["passed"])
        self.assertNotIn(A, str(summary))
        self.assertNotIn("I can help", str(summary))


if __name__ == "__main__":
    unittest.main()
