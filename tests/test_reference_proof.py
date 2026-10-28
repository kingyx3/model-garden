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
        "governance": {**proof._audit_summary(list(audit_rows)),
                       "completedCallActions": proof._completed_call_actions(list(voice_rows), list(audit_rows))},
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
        self.assertFalse(check(data, action=True)["passed"])  # unrelated execution cannot prove a voice action
        self.assertTrue(check(data, call=False, action=True)["passed"])  # standalone action mode is unchanged


    def test_a_completed_call_needs_a_matching_authorization_and_execution(self):
        call_rows = [voice("CallStarted"), voice("CallEnded", timestamp=110)]
        request_id = "c" * 64
        def row(kind, outcome, *, agent="receptionist", actor="runtime:hermes", tag="call:" + ROOM):
            return {"event": kind, "outcome": outcome, "requestId": request_id, "agent": agent,
                    "initiatingUser": actor, "governance": {"correlationId": tag}, "tool": "calendar.book"}
        authorized = row("authorization", "allow")
        succeeded = row("execution", "succeeded")
        data = evidence(call_rows, [authorized, succeeded])
        self.assertEqual(data["governance"]["completedCallActions"], 1)
        self.assertTrue(check(data, action=True)["passed"])
        self.assertEqual(evidence(call_rows, [succeeded])["governance"]["completedCallActions"], 0)
        self.assertEqual(evidence(call_rows, [authorized])["governance"]["completedCallActions"], 0)
        self.assertEqual(evidence(call_rows, [authorized, succeeded, succeeded])["governance"]["completedCallActions"], 1)

    def test_unrelated_agent_actor_or_conflicting_request_cannot_launder_call_proof(self):
        call_rows = [voice("CallStarted"), voice("CallEnded", timestamp=110)]
        rid = "d" * 64
        good = {"event": "authorization", "outcome": "allow", "requestId": rid, "agent": "receptionist",
                "initiatingUser": "runtime:hermes", "governance": {"correlationId": "call:" + ROOM}}
        executed = {**good, "event": "execution", "outcome": "succeeded"}
        for altered in (
            {"agent": "different-agent"},
            {"initiatingUser": "runtime:mailbox"},
            {"governance": {"correlationId": "call:" + OTHER}},
            {"governance": {"correlationId": "task:unrelated"}},
            {"governance": {"correlationId": None}},
        ):
            with self.subTest(altered=altered):
                data = evidence(call_rows, [good, {**executed, **altered}])
                self.assertEqual(data["governance"]["completedCallActions"], 0)
                self.assertFalse(check(data, action=True)["passed"])
        # A conflicting duplicate must veto even when another authorization/execution pair is valid.
        conflict = {**good, "governance": {"correlationId": "task:unrelated"}}
        self.assertEqual(evidence(call_rows, [good, executed, conflict])["governance"]["completedCallActions"], 0)

    def test_missing_call_or_request_provenance_cannot_satisfy_action_gate(self):
        call_rows = [voice("CallStarted"), voice("CallEnded", timestamp=110)]
        base = {"requestId": "e" * 64, "agent": "receptionist", "initiatingUser": "runtime:hermes",
                "governance": {"correlationId": "call:" + ROOM}}
        for bad in ({"requestId": None}, {"requestId": "invalid"}, {"agent": None},
                    {"initiatingUser": None}, {"governance": {}}, {"governance": {"correlationId": 123}}):
            with self.subTest(bad=bad):
                authorization = {**base, **bad, "event": "authorization", "outcome": "allow"}
                execution = {**base, **bad, "event": "execution", "outcome": "succeeded"}
                self.assertEqual(evidence(call_rows, [authorization, execution])["governance"]["completedCallActions"], 0)
        # A valid action in a call that never ended is not a completed-call action.
        valid = [{**base, "event": "authorization", "outcome": "allow"},
                 {**base, "event": "execution", "outcome": "succeeded"}]
        self.assertEqual(evidence([voice("CallStarted")], valid)["governance"]["completedCallActions"], 0)




    def test_collector_refuses_failed_or_malformed_evidence_reads(self):
        import subprocess
        from unittest import mock
        # Never treat an inaccessible audit log as an empty, successful read.
        with mock.patch.object(proof, "_run", return_value=subprocess.CompletedProcess([], 1, "", "sensitive error")):
            with self.assertRaisesRegex(ValueError, "evidence"):
                proof._jsonl("a" * 12, "/opt/data/.modelgarden/audit.jsonl")
        for raw in ('{"event":"authorization"}\\nnot-json\\n',
                    '{"event":"authorization"}\\n[]\\n',
                    '{"event":"authorization"}\\n{"event":'):
            with self.subTest(raw=raw):
                with mock.patch.object(proof, "_run", return_value=subprocess.CompletedProcess([], 0, raw, "")):
                    with self.assertRaisesRegex(ValueError, "evidence") as caught:
                        proof._jsonl("a" * 12, "/opt/data/.modelgarden/audit.jsonl")
                    self.assertNotIn("authorization", str(caught.exception))
                    self.assertNotIn("not-json", str(caught.exception))

    def test_duplicate_docker_service_cannot_hide_ambiguous_runtime(self):
        import subprocess
        from unittest import mock
        rows = "hermes|Up 1 minute|" + "a" * 12 + "\\nhermes|Up 2 minutes|" + "b" * 12 + "\\n"
        with mock.patch.object(proof, "_run", return_value=subprocess.CompletedProcess([], 0, rows, "")):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                proof._containers("acme-dev")

    def test_conflicting_approval_or_execution_cannot_prove_a_call_action(self):
        rid = "f" * 64
        base = {"requestId": rid, "agent": "receptionist", "initiatingUser": "runtime:hermes",
                "governance": {"correlationId": "call:" + ROOM}, "tool": "calendar.book"}
        authorized = {**base, "event": "authorization", "outcome": "require-approval"}
        approved = {**base, "event": "approval", "outcome": "approved"}
        rejected = {**base, "event": "approval", "outcome": "rejected"}
        success = {**base, "event": "execution", "outcome": "succeeded"}
        failure = {**base, "event": "execution", "outcome": "failed"}
        call = [voice("CallStarted"), voice("CallEnded", timestamp=110)]
        self.assertEqual(evidence(call, [authorized, approved, success])["governance"]["completedCallActions"], 1)
        self.assertEqual(evidence(call, [authorized, approved, rejected, success])["governance"]["completedCallActions"], 0)
        self.assertEqual(evidence(call, [authorized, approved, failure, success])["governance"]["completedCallActions"], 0)

    def test_required_service_must_be_observed_not_only_claimed_by_counts(self):
        data = evidence([voice("CallStarted"), voice("CallEnded", timestamp=110)])
        data["services"].pop("voice")
        self.assertFalse(check(data)["passed"])
        data = evidence(audit_rows=[{"event": "execution", "outcome": "succeeded"}])
        data["services"].pop("governed-tools")
        self.assertFalse(check(data, call=False, action=True)["passed"])

if __name__ == "__main__":
    unittest.main()
