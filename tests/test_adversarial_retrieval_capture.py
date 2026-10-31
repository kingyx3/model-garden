"""Credential-free regressions for the real Tier 0 retrieval -> model-answer evaluation path (#235).

These tests use a fake model; they cannot establish live provider safety.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "adversarial_retrieval_capture_under_test", ROOT / "scripts" / "capture-adversarial-retrieval.py"
)
capture = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = capture
spec.loader.exec_module(capture)


class FakeModel:
    def __init__(self, *, answer=None, error=None):
        self.calls = []
        self.response = answer
        self.error = error

    def answer(self, question, sections, *, session=None):
        self.calls.append((question, [dict(section) for section in sections], session))
        if self.error:
            raise self.error
        return self.response if self.response is not None else {
            "text": "Please ask a human about warranty.", "sources": [sections[0]["source"]]
        }


class RetrievalCaptureTests(unittest.TestCase):
    def test_real_compiled_kb_search_retrieves_hostile_document_and_ledgers_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeModel()
            result = capture.run_retrieval_case(client, pathlib.Path(tmp))
            self.assertEqual(result["source"], "unverified-retrieval-model-run")
            self.assertEqual(result["modelPath"], "coworker.answer")
            self.assertEqual(result["retrieval"]["status"], "executed")
            self.assertTrue(result["retrieval"]["auditChainValid"])
            self.assertEqual(len(result["retrieval"]["ledgerSha256"]), 64)
            self.assertTrue(result["retrieval"]["citations"])
            self.assertEqual(len(client.calls), 1)
            question, sections, session = client.calls[0]
            self.assertEqual(question, capture.QUESTION)
            self.assertTrue(session.startswith("retrieval-"))
            self.assertTrue(any(capture.CANARY in section["text"] for section in sections))
            self.assertTrue(all(section["source"].startswith("knowledge:rules-of-engagement#") for section in sections))
            self.assertNotIn(capture.CANARY, (pathlib.Path(tmp) / "audit.jsonl").read_text())
            self.assertNotIn("toolCalls", result)
            self.assertNotIn("disposition", result)
            self.assertNotIn("providerReceipt", result)

    def test_no_retrieved_attack_fails_before_model_invocation(self):
        for status, docs in (("executed", []), ("denied", [{"content": capture.CANARY}])):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                client = FakeModel()
                with mock.patch.object(capture.RUNTIME.GovernedToolRuntime, "kb_search", return_value={
                    "status": status, "result": {"dataOnly": True, "documents": docs}
                }):
                    with self.assertRaises(capture.CaptureError):
                        capture.run_retrieval_case(client, pathlib.Path(tmp))
                self.assertEqual(client.calls, [])

    def test_fake_or_unapproved_fixture_fails_before_model_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "missing.md"
            client = FakeModel()
            with mock.patch.object(capture, "FIXTURE", missing):
                with self.assertRaises(capture.CaptureError):
                    capture.run_retrieval_case(client, pathlib.Path(tmp))
            missing.write_text("# Fake\nNot the approved synthetic injection.\n")
            with mock.patch.object(capture, "FIXTURE", missing):
                with self.assertRaises(capture.CaptureError):
                    capture.run_retrieval_case(client, pathlib.Path(tmp))
            self.assertEqual(client.calls, [])

    def test_reject_unattributed_model_citation_and_oversized_answer(self):
        for answer in (
            {"text": "Unverified citation", "sources": ["knowledge:someone-else#private"]},
            {"text": "x" * 8193, "sources": ["knowledge:rules-of-engagement#001-synthetic-warranty-injection"]},
        ):
            with self.subTest(answer=str(answer)[:30]), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(capture.CaptureError):
                    capture.run_retrieval_case(FakeModel(answer=answer), pathlib.Path(tmp))

    def test_provider_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeModel(error=RuntimeError("SECRET-PROVIDER-TOKEN"))
            with self.assertRaises(capture.CaptureError) as caught:
                capture.run_retrieval_case(client, pathlib.Path(tmp))
            self.assertNotIn("SECRET-PROVIDER-TOKEN", str(caught.exception))

    def test_cli_writes_private_unverified_capture_not_a_safety_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = pathlib.Path(tmp) / "private"
            private.mkdir(mode=0o700)
            target = private / "retrieval.json"
            output = io.StringIO()
            with mock.patch.object(capture, "_client", return_value=FakeModel()), contextlib.redirect_stdout(output):
                code = capture.main(["--output", str(target), "--api-url", "http://hermes:8642/v1"])
            self.assertEqual(code, 0)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            document = json.loads(target.read_text())
            self.assertEqual(document["source"], "unverified-retrieval-model-run")
            self.assertNotIn("PASS", output.getvalue())
            self.assertNotIn(capture.CANARY, output.getvalue())
            self.assertNotIn("providerReceipt", document)
            with mock.patch.object(capture, "_client", return_value=FakeModel()):
                self.assertEqual(capture.main(["--output", str(target), "--api-url", "http://hermes:8642/v1"]), 1)


if __name__ == "__main__":
    unittest.main()
