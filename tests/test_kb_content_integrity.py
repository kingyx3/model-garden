"""Fail-closed Tier 0 retrieval when materialized knowledge no longer matches compilation (#116)."""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kb_runtime_integrity_cases", ROOT / "tests/test_kb_runtime.py")
kb_runtime = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(kb_runtime)


class KnowledgeIntegrityTests(unittest.TestCase):
    def runtime(self, root):
        return kb_runtime.RuntimeKbSearchTests().runtime(root)

    def test_valid_compiled_knowledge_still_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(pathlib.Path(tmp))
            answer = runtime.kb_search(query="opening hours")["result"]
            self.assertTrue(answer["documents"])

    def test_tampered_text_or_digest_never_reaches_caller_or_read_receipt(self):
        for mutation in ("content", "sha256", "sectionCount"):
            with self.subTest(mutation), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                runtime = self.runtime(root)
                entry = runtime.desired_state["knowledgeContent"]["rules-of-engagement"]
                if mutation == "content":
                    entry["content"] = "# Opening hours\nSECRET forged text opening hours\n"
                elif mutation == "sha256":
                    entry["sha256"] = "0" * 64
                else:
                    entry["sectionCount"] += 1
                result = runtime.kb_search(query="opening hours")["result"]
                self.assertEqual(result["documents"], [])
                self.assertEqual(result["reason"], "source-content-integrity")
                self.assertNotIn("SECRET forged", (root / "audit.jsonl").read_text())
                self.assertNotIn('"event": "read"', (root / "audit.jsonl").read_text())

    def test_missing_integrity_metadata_or_invalid_payload_fails_closed(self):
        for mutation in ("missing-digest", "missing-count", "nontext"):
            with self.subTest(mutation), tempfile.TemporaryDirectory() as tmp:
                runtime = self.runtime(pathlib.Path(tmp))
                entry = runtime.desired_state["knowledgeContent"]["rules-of-engagement"]
                if mutation == "missing-digest":
                    entry.pop("sha256")
                elif mutation == "missing-count":
                    entry.pop("sectionCount")
                else:
                    entry["content"] = {"not": "text"}
                result = runtime.kb_search(query="opening hours")["result"]
                self.assertEqual((result["documents"], result["reason"]),
                                 ([], "source-content-integrity"))


if __name__ == "__main__":
    unittest.main()
