"""Call-scoped evidence regressions for the LiveKit reference receptionist (#107).

No LiveKit, SIP, model or cloud credentials are used. These tests validate that
reused provider room names cannot merge two call transcripts or their receipts.
The call hash is a local correlation token, NOT provider/SIP attestation.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import pathlib
import re
import secrets
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKER = ROOT / "platform" / "channels" / "livekit_receptionist.py"
PROOF = ROOT / "scripts" / "reference-proof.py"
spec = importlib.util.spec_from_file_location("reference_proof_call_identity", PROOF)
proof = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = proof
spec.loader.exec_module(proof)

ROOM = "f" * 64
CALL_A = "a" * 64
CALL_B = "b" * 64
AGENT = "receptionist"


def lifecycle(call_hash, start, *, fallback=False):
    rows = [
        {"type": "CallStarted", "roomHash": ROOM, "callHash": call_hash,
         "agent": AGENT, "timestamp": start},
        {"type": "CallEnded", "roomHash": ROOM, "callHash": call_hash,
         "agent": AGENT, "timestamp": start + 10},
    ]
    if fallback:
        rows.append({"type": "FallbackMessage", "roomHash": ROOM,
                     "callHash": call_hash, "agent": AGENT, "timestamp": start + 5})
    return rows


def transcript(call_hash, start):
    return {
        "type": "CallTranscript", "callHash": call_hash, "agent": AGENT,
        "timestamp": start, "endedAt": start + 10, "summary": "A caller asked for help.",
        "summarySource": "extractive", "droppedTurns": 0,
        "disclosure": {"configured": True, "played": True},
        "turns": [
            {"index": 0, "speaker": "agent", "offsetSeconds": 0, "text": "Hello"},
            {"index": 1, "speaker": "caller", "offsetSeconds": 1, "text": "Please help"},
        ],
    }


def action(call_hash, request_id):
    shared = {
        "requestId": request_id, "agent": AGENT,
        "initiatingUser": "runtime:hermes", "tool": "crm.log-interaction",
        "governance": {"correlationId": "call:" + call_hash},
    }
    return [
        {**shared, "event": "authorization", "outcome": "allow"},
        {**shared, "event": "execution", "outcome": "succeeded"},
    ]


class CallIdentityTests(unittest.TestCase):
    def test_new_call_hash_is_unpredictable_and_not_reused_for_the_same_room(self):
        tree = ast.parse(WORKER.read_text(encoding="utf-8"))
        fn = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_call_hash")
        namespace = {"hashlib": hashlib, "secrets": secrets}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(WORKER), "exec"), namespace)
        with mock.patch.object(secrets, "token_bytes", side_effect=[b"A" * 32, b"B" * 32]) as random:
            first = namespace["_call_hash"]("same-provider-room")
            second = namespace["_call_hash"]("same-provider-room")
        self.assertEqual(random.call_count, 2)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertRegex(second, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, hashlib.sha256(b"same-provider-room").hexdigest())

    def test_worker_uses_invocation_identity_for_transcript_and_lifecycle(self):
        tree = ast.parse(WORKER.read_text(encoding="utf-8"))
        worker = next(node for node in tree.body
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == "receptionist")
        source = ast.unparse(worker)
        self.assertIn("call_hash = _call_hash(ctx.room.name)", source)
        self.assertIn("CallTranscript(call_hash,", source)
        self.assertIn("'callHash': call_hash", source)
        self.assertIn("call_hash=call_hash", source)

    def test_reused_room_is_two_completed_calls_and_two_transcripts(self):
        rows = lifecycle(CALL_A, 100, fallback=True) + lifecycle(CALL_B, 200)
        counts = proof._voice_summary(rows)
        self.assertEqual(counts["distinctCalls"], 2)
        self.assertEqual(counts["completedCalls"], 2)
        self.assertEqual(counts["completedCallFallbackEvidence"], 1)
        self.assertEqual(
            proof._transcript_summary(rows, [transcript(CALL_A, 100), transcript(CALL_B, 200)])["completeTwoWayCalls"], 2
        )
        self.assertEqual(
            proof._completed_call_actions(rows, action(CALL_A, "1" * 64) + action(CALL_B, "2" * 64)), 2
        )

    def test_malformed_explicit_call_identity_does_not_fall_back_to_room(self):
        rows = lifecycle(CALL_A, 100)
        rows[1]["callHash"] = "not-a-hash"
        self.assertEqual(proof._voice_summary(rows)["completedCalls"], 0)
        self.assertEqual(proof._transcript_summary(rows, [transcript(CALL_A, 100)])["completeTwoWayCalls"], 0)
        self.assertEqual(proof._completed_call_actions(rows, action(CALL_A, "1" * 64)), 0)

    def test_legacy_room_only_receipts_remain_readable(self):
        rows = [{k: v for k, v in row.items() if k != "callHash"} for row in lifecycle(ROOM, 100)]
        self.assertEqual(proof._voice_summary(rows)["completedCalls"], 1)
        self.assertEqual(proof._transcript_summary(rows, [transcript(ROOM, 100)])["completeTwoWayCalls"], 1)


if __name__ == "__main__":
    unittest.main()
