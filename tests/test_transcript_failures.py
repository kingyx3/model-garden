"""Credential-free checks for ledgering call-transcript write failures reported by the voice worker."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


filing = _load("filing_for_failures", "platform/channels/conversation_filing.py")
govern = _load("govern_for_failures", "scripts/govern-action.py")
compiler = _load("compiler_for_failures", "scripts/compile-workspace.py")
runtime_module = _load("runtime_for_failures", "platform/runtime/governed_mcp.py")
cli = _load("file_calls_for_failures", "scripts/runtime-file-calls.py")
operator = _load("operator_for_failures", "scripts/client-operator.py")

CALL_HASH = "ab" * 32


def failure(at=1_790_000_000, *, error="OSError", room=CALL_HASH, **extra) -> dict:
    return {"type": "TranscriptWriteFailed", "timestamp": at, "roomHash": room, "agent": "model-garden-receptionist", "errorType": error, **extra}


class ReadTests(unittest.TestCase):
    def test_only_valid_failure_events_are_read_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "messages.jsonl"
            path.write_text("\n".join([
                json.dumps(failure(30)), json.dumps({"type": "CallEnded", "timestamp": 1}),
                json.dumps(failure(10, room="cd" * 32)), "",
            ]), encoding="utf-8")
            self.assertEqual([f["timestamp"] for f in filing.read_transcript_failures(path)], [10, 30])
            self.assertEqual(filing.read_transcript_failures(pathlib.Path(tmp) / "missing"), [])
            for invalid in ("not json\n", "[]\n", '{"type":"TranscriptWriteFailed"}\n'):
                path.write_text(json.dumps(failure(30)) + "\n" + invalid, encoding="utf-8")
                with self.assertRaises(ValueError):
                    filing.read_transcript_failures(path)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audit = pathlib.Path(self._tmp.name) / "audit.jsonl"

    def events(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def test_each_failure_is_ledgered_once_with_error_type_time_and_call_hash_only(self):
        totals = filing.ledger_transcript_failures(govern, self.audit, [failure(10), failure(20, error="PermissionError")])
        self.assertEqual(totals, {"ledgered": 2, "known": 0})
        first, second = self.events()
        self.assertEqual((first["action"], first["performedBy"]), ("transcript.write-failed", "runtime:conversation-filing"))
        self.assertEqual({k: v for k, v in first["details"].items() if k != "fingerprint"},
                         {"at": 10, "errorType": "OSError", "callHash": CALL_HASH})
        self.assertEqual(second["details"]["errorType"], "PermissionError")
        self.assertTrue(govern.verify_audit_chain(self.audit)["valid"])

    def test_running_again_over_the_same_log_records_nothing_new_and_new_failures_are_added(self):
        filing.ledger_transcript_failures(govern, self.audit, [failure(10)])
        again = filing.ledger_transcript_failures(govern, self.audit, [failure(10), failure(11)])
        self.assertEqual(again, {"ledgered": 1, "known": 1})
        self.assertEqual(len(self.events()), 2)

    def test_the_same_failure_twice_in_one_batch_is_one_event(self):
        self.assertEqual(filing.ledger_transcript_failures(govern, self.audit, [failure(10), failure(10)]), {"ledgered": 1, "known": 1})

    def test_untrusted_fields_cannot_inject_text_or_oversized_values_into_the_ledger(self):
        hostile = failure(10, error="OSError: /home/alex/secret.txt not found", room="not-a-hash", message="Alex Tan's number is 6123 4567")
        filing.ledger_transcript_failures(govern, self.audit, [hostile])
        (event,) = self.events()
        self.assertEqual(event["details"]["errorType"], "Unknown")
        self.assertNotIn("callHash", event["details"])
        text = self.audit.read_text()
        for private in ("alex", "Alex", "6123 4567", "61234567", "secret"):
            self.assertNotIn(private, text)

    def test_corrupt_audit_line_blocks_deduplication_and_new_receipts(self):
        filing.ledger_transcript_failures(govern, self.audit, [failure(10)])
        with self.audit.open("a", encoding="utf-8") as handle:
            handle.write("garbage\n")
        original = self.audit.read_bytes()
        with self.assertRaisesRegex(ValueError, "Audit chain invalid"):
            filing.ledger_transcript_failures(govern, self.audit, [failure(10), failure(11)])
        self.assertEqual(self.audit.read_bytes(), original)

    def test_valid_json_with_broken_chain_cannot_be_used_as_a_known_failure(self):
        filing.ledger_transcript_failures(govern, self.audit, [failure(10)])
        rows = self.events()
        rows[0]["details"]["errorType"] = "ForgedError"
        self.audit.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
        original = self.audit.read_bytes()
        with self.assertRaisesRegex(ValueError, "Audit chain invalid"):
            filing.ledger_transcript_failures(govern, self.audit, [failure(10)])
        self.assertEqual(self.audit.read_bytes(), original)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        binding = {"environment": "dev", "connectors": {"crm": {"provider": "built-in-crm", "tier": 0},
                                                         "kb": {"provider": "text-knowledge", "tier": 0},
                                                         "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://g/dev"}}}
        desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist", environment_binding=binding)[0]
        (self.root / "desired.json").write_text(json.dumps(desired), encoding="utf-8")
        manifest = runtime_module.CAPABILITIES.build_manifest(desired, binding)
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def run_cli(self, stdin: str):
        out, err = io.StringIO(), io.StringIO()
        env = {"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")}
        argv = ["--transcripts", "-", "--desired-state", str(self.root / "desired.json"), "--audit-path", str(self.root / "audit.jsonl"),
                "--approval-root", str(self.root / "approvals")]
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_failures_in_the_same_stream_as_transcripts_are_ledgered_once_per_run_and_reported(self):
        stream = "\n".join([json.dumps(failure(10)), json.dumps(failure(20, error="PermissionError"))]) + "\n"
        code, out, _ = self.run_cli(stream)
        self.assertEqual((code, json.loads(out)["writeFailures"], json.loads(out)["records"]), (0, {"ledgered": 2, "known": 0}, 0))
        code, out, _ = self.run_cli(stream)
        self.assertEqual((code, json.loads(out)["writeFailures"]), (0, {"ledgered": 0, "known": 2}))
        events = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(len([e for e in events if e.get("action") == "transcript.write-failed"]), 2)
        self.assertTrue(govern.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_file_calls_refuses_corrupt_ledger_before_processing_voice_records(self):
        audit = self.root / "audit.jsonl"
        filing.ledger_transcript_failures(govern, audit, [failure(10)])
        event = json.loads(audit.read_text(encoding="utf-8"))
        event["details"]["errorType"] = "ForgedError"
        audit.write_text(json.dumps(event) + "\n", encoding="utf-8")
        original = audit.read_bytes()
        code, output, error = self.run_cli(json.dumps(failure(11)) + "\n")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Audit chain invalid", error)
        self.assertEqual(audit.read_bytes(), original)

    def test_untrusted_audit_fields_never_leak_into_cli_error_output(self):
        audit = self.root / "audit.jsonl"
        audit.write_text(json.dumps({"schemaVersion": "customer-secret@example.invalid"}) + "\n", encoding="utf-8")
        code, output, error = self.run_cli(json.dumps(failure(11)) + "\n")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Audit chain invalid", error)
        self.assertNotIn("customer-secret", error)

    def test_the_operator_command_reads_the_transcripts_and_the_lifecycle_log_together(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return mock.Mock(returncode=0, stdout="x1\n", stderr="")
            return mock.Mock(returncode=0, stdout="{}\n", stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            operator.runtime_file_calls("acme-dev")
        read = next(c for c in calls if c[:3] == ["docker", "exec", "x1"] and "cat" in " ".join(c))
        self.assertIn("/opt/data/transcripts.jsonl", " ".join(read))
        self.assertIn("/opt/data/messages.jsonl", " ".join(read))


if __name__ == "__main__":
    unittest.main()
