"""Call-scoped evidence regressions for the LiveKit reference receptionist (#107).

No LiveKit, SIP, model or cloud credentials are used. These tests validate that
reused provider room names cannot merge two call transcripts or their receipts.
The call hash is a local correlation token, NOT provider/SIP attestation.
"""
from __future__ import annotations

import ast
import asyncio
import os
import time
import hashlib
import importlib.util
import pathlib
import re
import secrets
import sys
import unittest
from types import SimpleNamespace
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

    def test_ambiguous_sip_room_cannot_select_an_arbitrary_caller(self):
        # Execute the real LiveKit worker helper without importing the SIP SDK.
        # A room may have multiple remote SIP participants (conference/reconnect);
        # selecting the first would transfer the wrong caller or leak their number.
        tree = ast.parse(WORKER.read_text(encoding="utf-8"))
        helper = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_sip_participant")
        rtc = SimpleNamespace(ParticipantKind=SimpleNamespace(SIP="sip"))
        scope = {"JobContext": object, "rtc": rtc}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(WORKER), "exec"), scope)
        select = scope["_sip_participant"]
        alice = SimpleNamespace(kind="sip", identity="alice")
        bob = SimpleNamespace(kind="sip", identity="bob")
        observer = SimpleNamespace(kind="other", identity="observer")

        def room(*participants):
            return SimpleNamespace(room=SimpleNamespace(
                remote_participants={str(i): p for i, p in enumerate(participants)}
            ))

        self.assertIs(select(room(observer, alice)), alice)
        self.assertIsNone(select(room(observer)))
        for participants in ((alice, bob), (bob, alice), (observer, alice, bob)):
            with self.subTest(participants=participants):
                with self.assertRaisesRegex(ValueError, "ambiguous SIP"):
                    select(room(*participants))

        # Both SIP transfer and callback-number capture must use this same
        # fail-closed selector rather than independently choosing a participant.
        agent = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "ReceptionistVoiceAgent")
        for method in ("transfer_to_human", "capture_message"):
            implementation = next(node for node in agent.body
                                  if isinstance(node, ast.AsyncFunctionDef) and node.name == method)
            self.assertIn("_sip_participant(job_ctx)", ast.unparse(implementation))

    def test_multi_sip_room_refuses_transfer_and_message_before_any_effect(self):
        # The real worker methods run with only transport fakes. No provider
        # identity, phone number, message or SIP transfer may be guessed.
        tree = ast.parse(WORKER.read_text(encoding="utf-8"))
        helper = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_sip_participant")
        agent_node = next(node for node in tree.body
                          if isinstance(node, ast.ClassDef) and node.name == "ReceptionistVoiceAgent")
        alice = SimpleNamespace(kind="sip", identity="alice")
        bob = SimpleNamespace(kind="sip", identity="bob")
        transfers, writes = [], []

        async def transfer(request):
            transfers.append(request)

        async def active():
            return False

        job = SimpleNamespace(
            room=SimpleNamespace(name="room", remote_participants={"a": alice, "b": bob}),
            api=SimpleNamespace(sip=SimpleNamespace(transfer_sip_participant=transfer)),
        )
        class BaseAgent:
            def __init__(self, *, instructions):
                pass

        scope = {
            "JobContext": object,
            "rtc": SimpleNamespace(ParticipantKind=SimpleNamespace(SIP="sip")),
            "Agent": BaseAgent,
            "function_tool": lambda: (lambda fn: fn),
            "get_job_context": lambda: job,
            "voice_paused": active,
            "HOLDING": "paused",
            "os": os,
            "time": time,
            "api": SimpleNamespace(TransferSIPParticipantRequest=lambda **kw: kw),
            "AGENT_NAME": "receptionist",
            "_safe_digest": lambda value: "digest",
            "_append_message": lambda payload: writes.append(payload),
        }
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            helper, agent_node,
        ], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(WORKER), "exec"), scope)
        agent = scope["ReceptionistVoiceAgent"]("receptionist", call_hash="a" * 64)
        with mock.patch.dict(os.environ, {"MODEL_GARDEN_HUMAN_TRANSFER_TARGET": "tel:+1234567"}):
            self.assertIn("multiple SIP", asyncio.run(agent.transfer_to_human(None)))
            self.assertIn("multiple SIP", asyncio.run(agent.capture_message(None, "private message")))
        self.assertEqual(transfers, [])
        self.assertEqual(writes, [])

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


    def test_write_failure_dedupe_uses_invocation_not_reused_room(self):
        filing_spec = importlib.util.spec_from_file_location(
            "filing_call_identity", ROOT / "platform" / "channels" / "conversation_filing.py"
        )
        filing = importlib.util.module_from_spec(filing_spec)
        assert filing_spec.loader is not None
        sys.modules[filing_spec.name] = filing
        filing_spec.loader.exec_module(filing)
        base = {"type": "TranscriptWriteFailed", "timestamp": 100, "roomHash": ROOM, "errorType": "OSError"}
        self.assertNotEqual(
            filing._failure_fingerprint({**base, "callHash": CALL_A}),
            filing._failure_fingerprint({**base, "callHash": CALL_B}),
        )
        self.assertEqual(filing._failure_identity(base), ROOM)
        with self.assertRaises(ValueError):
            filing._failure_fingerprint({**base, "callHash": "spoofed"})



if __name__ == "__main__":
    unittest.main()
