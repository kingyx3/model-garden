"""Credential-free checks that `docs.create` builds real DOCX/XLSX/PPTX/PDF/CSV files through the governed boundary (#101, #77, #91)."""
from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("docs_runtime_cases_for_formats", ROOT / "tests" / "test_docs_runtime.py")
base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = base
_spec.loader.exec_module(base)

PRIVATE = "Hargreaves boiler at 12 Acacia Road"
REPORT = {"sections": [{"heading": "Summary", "paragraphs": ["Hours are 9 to 5.", PRIVATE]}]}
SHEET = {"sheets": [{"name": "Bookings", "rows": [["Name", "Total"], ["=HYPERLINK(\"http://x\")", 12]]}]}
DECK = {"slides": [{"title": "Plan", "bullets": ["One", "Two"]}]}
ROWS = {"rows": [["a", "b"], ["=1+1", 2]]}


class FormatTests(base.Case):
    def make(self, runtime, **kwargs):
        pending = runtime.docs_create(**kwargs)
        self.assertEqual(pending["status"], "pending-approval")
        self.approve(pending)
        return runtime.docs_create(**kwargs)

    def stored(self, result):
        artifact = result["result"]["artifactId"]
        entry, data = self.store().get(artifact, requested_by="alice@acme.example")
        return entry, data

    def test_each_format_lands_in_the_store_as_a_real_file(self):
        runtime = self.runtime()
        cases = (("docx", REPORT, "word/document.xml"), ("pdf", REPORT, None), ("pptx", DECK, "ppt/presentation.xml"),
                 ("xlsx", SHEET, "xl/workbook.xml"), ("csv", ROWS, None))
        for kind, structure, member in cases:
            with self.subTest(kind):
                result = self.make(runtime, title=f"Weekly {kind}", format=kind, structure=structure, justification="asked for a file")
                self.assertEqual(result["status"], "executed")
                document = result["result"]
                self.assertEqual(document["format"], kind)
                self.assertTrue(document["documentId"].startswith("tier0:acme/a-"))
                entry, data = self.stored(result)
                self.assertTrue(entry["name"].endswith("." + kind))
                self.assertEqual(entry["mediaType"], runtime_media[kind])
                if member:
                    self.assertIn(member, zipfile.ZipFile(io.BytesIO(data)).namelist())
                elif kind == "pdf":
                    self.assertTrue(data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF"))

    def test_spreadsheet_text_is_never_a_formula(self):
        runtime = self.runtime()
        result = self.make(runtime, title="Bookings", format="xlsx", structure=SHEET)
        _, data = self.stored(result)
        archive = zipfile.ZipFile(io.BytesIO(data))
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("<f>", sheet)
        self.assertIn("HYPERLINK", sheet)
        csv_result = self.make(runtime, title="Rows", format="csv", structure=ROWS)
        _, csv_data = self.stored(csv_result)
        self.assertIn("'=1+1", csv_data.decode())

    def test_sources_are_recorded_with_the_artifact_and_the_ledger_has_no_text(self):
        runtime = self.runtime()
        self.make(runtime, title="Hours report", format="docx", structure=REPORT, sources=["kb:rules-of-engagement#hours"])
        (entry,) = self.store().list()
        self.assertEqual(entry["sources"], ["kb:rules-of-engagement#hours"])
        created = next(event for event in self.audit() if event.get("action") == "artifact.create")
        self.assertEqual(created["details"]["sources"], ["kb:rules-of-engagement#hours"])
        execution = next(event for event in self.audit() if event["event"] == "execution")
        self.assertEqual(execution["ledger"]["externalObjectRef"], created["details"]["location"])
        self.assertEqual(execution["arguments"]["format"], "docx")
        self.assertEqual(execution["arguments"]["structure"], base.runtime_module.GOVERN.REDACTED)
        text = (self.root / "audit.jsonl").read_text()
        for private in ("Hargreaves", "Acacia", "Hours are"):
            self.assertNotIn(private, text)
        self.assertTrue(base.runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_the_approval_binds_the_exact_format_structure_and_sources(self):
        runtime = self.runtime()
        pending = runtime.docs_create(title="Report", format="docx", structure=REPORT)
        self.approve(pending)
        for changed in ({"format": "pdf", "structure": REPORT},
                        {"format": "docx", "structure": {"sections": [{"heading": "Other", "paragraphs": ["x"]}]}},
                        {"format": "docx", "structure": REPORT, "sources": ["kb:x"]}):
            again = runtime.docs_create(title="Report", **changed)
            self.assertEqual(again["status"], "pending-approval", changed)
        self.assertEqual(self.store().list(), [])

    def test_the_same_file_from_the_same_person_is_not_stored_twice(self):
        runtime = self.runtime()
        first = self.make(runtime, title="Deck", format="pptx", structure=DECK)["result"]
        again = self.make(runtime, title="Deck", format="pptx", structure=DECK)["result"]
        self.assertEqual((again["documentId"], again["duplicate"]), (first["documentId"], True))
        self.assertEqual(len(self.store().list()), 1)

    def test_bad_input_fails_closed_and_stores_nothing(self):
        runtime = self.runtime()
        bad = (
            {"format": "exe", "structure": REPORT},
            {"format": "docx", "structure": SHEET},
            {"format": "docx", "structure": None},
            {"format": "docx", "structure": REPORT, "content": "also text"},
            {"format": "markdown", "content": "text", "structure": REPORT},
            {"format": "xlsx", "structure": {"sheets": []}},
            {"format": "pptx", "structure": {"slides": [{"title": "", "bullets": []}]}},
            {"format": "docx", "structure": REPORT, "sources": ["has space"]},
            {"format": "docx", "structure": REPORT, "sources": ["kb:x"] * 21},
        )
        for kwargs in bad:
            with self.assertRaises(ValueError, msg=str(kwargs)[:80]):
                self.make(runtime, title="Report", **kwargs)
        self.assertEqual(self.store().list(), [])

    def test_markdown_requests_are_unchanged(self):
        runtime = self.runtime()
        result = self.make(runtime, title="Notes", content="# Notes\n\nOK")
        self.assertEqual(result["result"]["format"], "markdown")
        entry, _ = self.stored(result)
        self.assertEqual(entry["name"], "Notes.md")

    def test_public_callers_and_unbound_stores_are_still_refused(self):
        public = base.principal_for("U-STRANGER")
        with self.assertRaises(PermissionError):
            self.runtime(principal=public).docs_create(title="Report", format="docx", structure=REPORT)
        self.assertEqual(self.store().list(), [])
        with self.assertRaises(PermissionError):
            self.runtime(binding=None).docs_create(title="Report", format="docx", structure=REPORT)


runtime_media = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "csv": "text/csv",
}

if __name__ == "__main__":
    unittest.main()
