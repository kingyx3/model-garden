"""Credential-free checks for the PPTX and PDF deliverable builders (#101)."""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


artifacts = _load("artifacts_for_formats", "platform/core/artifacts.py")
deliverables = _load("deliverables_for_formats", "platform/core/deliverables.py")
DError = deliverables.DeliverableError
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/package/2006/relationships}"
HOSTILE = "<script>alert(1)</script> & \"quotes\" =HYPERLINK(\"http://evil.example\") \x00\x07 end"
SLIDES = [{"title": "Highlights", "bullets": ["Revenue up 12%", HOSTILE]}, {"title": "Next steps", "bullets": []}]
SECTIONS = [{"heading": "Summary", "paragraphs": ["Three open deals.", "Hargreaves (Co) \\ owes 4,200 SGD"]}, {"paragraphs": ["Closing note"]}]


def pptx_texts(data: bytes, number: int) -> list[str]:
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read(f"ppt/slides/slide{number}.xml"))
    return ["".join(node.text or "" for node in paragraph.iter(f"{A}t")) for paragraph in root.iter(f"{A}p")]


class PptxTests(unittest.TestCase):
    def test_a_deck_is_a_well_formed_package_with_a_title_slide_and_the_slides_in_order(self):
        data = deliverables.build_pptx("Q3 review", SLIDES)
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(archive.testzip())
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(archive.read(name))  # every part parses
        self.assertEqual(pptx_texts(data, 1), ["Q3 review"])
        self.assertEqual(pptx_texts(data, 2)[:2], ["Highlights", "Revenue up 12%"])
        self.assertEqual(pptx_texts(data, 3)[0], "Next steps")
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        self.assertEqual(len(list(presentation.iter(f"{P}sldId"))), 3)

    def test_every_relationship_target_exists_and_every_part_has_a_content_type(self):
        archive = zipfile.ZipFile(io.BytesIO(deliverables.build_pptx("T", SLIDES)))
        names = set(archive.namelist())
        for name in names:
            if name.endswith(".rels"):
                base = pathlib.PurePosixPath(name).parent.parent
                for relationship in ET.fromstring(archive.read(name)).iter(f"{R}Relationship"):
                    target = (base / relationship.get("Target")).as_posix()
                    resolved = pathlib.PurePosixPath(*[part for part in pathlib.PurePosixPath(target).parts if part != "."])
                    parts: list[str] = []
                    for part in resolved.parts:
                        parts.append(part) if part != ".." else parts.pop()
                    self.assertIn("/".join(parts), names, (name, relationship.get("Target")))
        types = archive.read("[Content_Types].xml").decode()
        for name in names:
            if name.startswith("ppt/") and name.endswith(".xml"):
                self.assertIn(f'PartName="/{name}"', types, name)

    def test_hostile_text_stays_inert_text_and_the_deck_carries_no_active_content(self):
        data = deliverables.build_pptx("T", SLIDES)
        text = pptx_texts(data, 2)[2]
        self.assertIn("<script>alert(1)</script>", text)  # present as text...
        archive = zipfile.ZipFile(io.BytesIO(data))
        joined = b"".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(b"<script>", joined)  # ...and escaped in the markup
        self.assertNotIn(b"\x00", joined)
        for forbidden in (b"hlinkClick", b"vbaProject", b"macro", b"oleObject", b"externalLink", b'TargetMode="External"', b"embeddings"):
            self.assertNotIn(forbidden, joined, forbidden)

    def test_the_output_is_deterministic(self):
        self.assertEqual(deliverables.build_pptx("T", SLIDES), deliverables.build_pptx("T", SLIDES))
        self.assertNotEqual(deliverables.build_pptx("T", SLIDES), deliverables.build_pptx("T2", SLIDES))

    def test_invalid_or_oversized_decks_are_refused(self):
        for bad in (lambda: deliverables.build_pptx("", SLIDES), lambda: deliverables.build_pptx("T", []), lambda: deliverables.build_pptx("T", "slides"),
                    lambda: deliverables.build_pptx("T", [{"bullets": ["x"]}]), lambda: deliverables.build_pptx("T", [{"title": " "}]),
                    lambda: deliverables.build_pptx("T", [{"title": "a", "bullets": "x"}]), lambda: deliverables.build_pptx("T", [{"title": "a", "bullets": [1]}]),
                    lambda: deliverables.build_pptx("T", [{"title": "a", "bullets": ["x"] * 21}]),
                    lambda: deliverables.build_pptx("T", [{"title": "a", "bullets": ["x" * 1001]}]),
                    lambda: deliverables.build_pptx("T", [{"title": "a"}] * 101), lambda: deliverables.build_pptx("T", ["a"])):
            with self.assertRaises(DError):
                bad()


def pdf_objects(data: bytes) -> dict[int, bytes]:
    return {int(number): body for number, body in re.findall(rb"(\d+) 0 obj\n(.*?)\nendobj\n", data, re.DOTALL)}


class PdfTests(unittest.TestCase):
    def test_a_pdf_has_a_valid_cross_reference_table_and_the_text_in_order(self):
        data = deliverables.build_pdf("Pipeline summary", SECTIONS)
        self.assertTrue(data.startswith(b"%PDF-1.4\n") and data.endswith(b"%%EOF\n"))
        start = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
        self.assertTrue(data[start:].startswith(b"xref\n"))
        entries = re.findall(rb"(\d{10}) 00000 n \n", data[start:])
        objects = pdf_objects(data)
        self.assertEqual(len(entries), len(objects))
        for number, offset in enumerate(entries, start=1):
            self.assertTrue(data[int(offset):].startswith(f"{number} 0 obj\n".encode()), number)
        text = data.decode("latin-1")
        self.assertLess(text.index("Pipeline summary"), text.index("Summary"))
        self.assertLess(text.index("Three open deals."), text.index("Closing note"))

    def test_each_stream_length_is_exact(self):
        data = deliverables.build_pdf("T", SECTIONS)
        for match in re.finditer(rb"/Length (\d+) >>\nstream\n(.*?)\nendstream", data, re.DOTALL):
            self.assertEqual(int(match.group(1)), len(match.group(2)))

    def test_text_cannot_escape_its_string_or_add_operators_actions_or_scripts(self):
        hostile = "a) Tj ET /JS (app.alert(1)) /OpenAction << /S /JavaScript >> \\ ("
        data = deliverables.build_pdf("T", [{"paragraphs": [hostile, HOSTILE]}])
        for forbidden in (b"/JS", b"/JavaScript", b"/OpenAction", b"/AA", b"/Launch", b"/URI", b"/EmbeddedFile", b"/AcroForm"):
            # the words may appear only inside an escaped text string, never as a name object outside one
            outside = re.sub(rb"\((?:\\.|[^\\)])*\)", b"()", data)
            self.assertNotIn(forbidden, outside, forbidden)
        self.assertIn(rb"a\) Tj ET /JS \(app.alert\(1\)\)", data)
        self.assertNotIn(b"\x00", data.split(b"stream\n", 1)[1])

    def test_long_text_wraps_inside_the_margins_and_paginates(self):
        data = deliverables.build_pdf("Long", [{"heading": "H", "paragraphs": ["word " * 4000, "x" * 5000]}])
        pages = len(re.findall(rb"/Type /Page /Parent", data))
        self.assertGreater(pages, 2)
        self.assertIn(f"/Count {pages}".encode(), data)
        longest = max(len(line) for line in re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", data))
        self.assertLessEqual(longest, 100)
        for y in (int(value) for value in re.findall(rb" (\d+) Td ", data)):
            self.assertGreaterEqual(y, 56)

    def test_non_latin_text_degrades_to_question_marks_and_never_breaks_the_file(self):
        data = deliverables.build_pdf("Café", [{"paragraphs": ["naïve £5 ☃ 日本語"]}])
        self.assertIn("Café".encode("latin-1"), data)
        self.assertIn(b"na\xefve \xa35 ? ???", data)
        data.decode("latin-1")

    def test_the_output_is_deterministic_and_carries_no_dates_or_producer(self):
        self.assertEqual(deliverables.build_pdf("T", SECTIONS), deliverables.build_pdf("T", SECTIONS))
        for forbidden in (b"CreationDate", b"ModDate", b"Producer", b"Creator"):
            self.assertNotIn(forbidden, deliverables.build_pdf("T", SECTIONS))

    def test_invalid_or_oversized_documents_are_refused(self):
        for bad in (lambda: deliverables.build_pdf("", SECTIONS), lambda: deliverables.build_pdf("T", []), lambda: deliverables.build_pdf("T", [{"paragraphs": [1]}]),
                    lambda: deliverables.build_pdf("T", [{"heading": " ", "paragraphs": []}]), lambda: deliverables.build_pdf("T", [{"paragraphs": "x"}]),
                    lambda: deliverables.build_pdf("T", [{"paragraphs": ["x" * 20001]}]),
                    lambda: deliverables.build_pdf("T", [{"paragraphs": ["line " * 3500] * 150}])):
            with self.assertRaises(DError):
                bad()


class DeliverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple] = []
        self.store = artifacts.ArtifactStore(pathlib.Path(self._tmp.name), tenant="acme-prod", audit=lambda *event: self.events.append(event))

    def deliver(self, kind, content, name="Quarterly review"):
        return deliverables.deliver(self.store, kind=kind, name=name, content=content, agent="account-researcher", requested_by="alice@client.example",
                                    sources=["crm:contact/c-1"], now=1_800_000_000)

    def test_a_deck_and_a_pdf_are_stored_with_their_type_digest_and_sources_and_no_content_in_the_ledger(self):
        deck = self.deliver("pptx", {"title": "Q3 review", "slides": SLIDES})
        pdf = self.deliver("pdf", {"title": "Q3 report", "sections": SECTIONS})
        self.assertEqual((deck["name"], deck["mediaType"]), ("Quarterly review.pptx", deliverables.PPTX_TYPE))
        self.assertEqual((pdf["name"], pdf["mediaType"]), ("Quarterly review.pdf", deliverables.PDF_TYPE))
        _, data = self.store.get(pdf["id"], requested_by="alice@client.example", now=1_800_000_000)
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertEqual([event[2]["sources"] for event in self.events if event[0] == "artifact.create"], [["crm:contact/c-1"]] * 2)
        self.assertNotIn("Hargreaves", json.dumps(self.events))

    def test_an_unknown_kind_or_bad_content_stores_nothing(self):
        before = len(self.store.list())
        for kind, content in (("odt", {}), ("pptx", {"title": "T", "slides": []}), ("pdf", {"title": "", "sections": SECTIONS}), ("pptx", None)):
            with self.subTest(kind=kind), self.assertRaises(DError):
                self.deliver(kind, content)
        self.assertEqual(len(self.store.list()), before)


if __name__ == "__main__":
    unittest.main()
