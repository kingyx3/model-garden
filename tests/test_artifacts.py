"""Credential-free checks for the Tier 0 artifact store and the DOCX/XLSX/CSV deliverable builders."""
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
import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


artifacts = _load("artifacts_under_test", "platform/core/artifacts.py")
deliverables = _load("deliverables_under_test", "platform/core/deliverables.py")
govern = _load("govern_for_artifacts", "scripts/govern-action.py")
cli = _load("runtime_artifacts_cli", "scripts/runtime-artifacts.py")
operator = _load("operator_for_artifacts", "scripts/client-operator.py")
Error, DError = artifacts.ArtifactError, deliverables.DeliverableError
ALICE, BOB, ADMIN = "alice@client.example", "bob@client.example", "owner@client.example"
T0 = 1_800_000_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SOURCES = ["kb:rules-of-engagement#hours", "crm:contact/c-1"]
SECRET_TEXT = "Hargreaves & Co owes 4,200 SGD"


def open_zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


class BuilderCase(unittest.TestCase):
    def test_a_docx_is_a_valid_package_with_the_text_in_order(self):
        data = deliverables.build_docx("Pipeline summary", [
            {"heading": "Open deals", "paragraphs": ["Three deals open.", SECRET_TEXT]}, {"paragraphs": ["Closing note"]},
        ])
        archive = open_zip(data)
        self.assertIsNone(archive.testzip())
        self.assertEqual(sorted(archive.namelist()), ["[Content_Types].xml", "_rels/.rels", "word/document.xml"])
        for name in archive.namelist():
            ET.fromstring(archive.read(name))  # every part is well-formed XML
        root = ET.fromstring(archive.read("word/document.xml"))
        texts = ["".join(node.text or "" for node in paragraph.iter(f"{W}t")) for paragraph in root.iter(f"{W}p")]
        self.assertEqual(texts, ["Pipeline summary", "Open deals", "Three deals open.", SECRET_TEXT, "Closing note"])
        styles = [paragraph.find(f"{W}pPr/{W}pStyle").get(f"{W}val") for paragraph in root.iter(f"{W}p") if paragraph.find(f"{W}pPr") is not None]
        self.assertEqual(styles, ["Title", "Heading1"])

    def test_markup_and_control_characters_cannot_break_the_xml(self):
        hostile = "<w:p>&amp;</w:p>\x00\x07 \"quoted\" </w:t>"
        data = deliverables.build_docx("T", [{"heading": hostile, "paragraphs": [hostile]}])
        root = ET.fromstring(open_zip(data).read("word/document.xml"))
        text = [node.text for node in root.iter(f"{W}t")]
        self.assertIn('<w:p>&amp;</w:p> "quoted" </w:t>', text)
        self.assertNotIn("\x00", open_zip(data).read("word/document.xml").decode())

    def test_an_xlsx_has_sheets_numbers_and_text_that_never_becomes_a_formula(self):
        data = deliverables.build_xlsx([
            {"name": "Pipeline", "rows": [["Customer", "Amount", "Won"], ["Hargreaves & Co", 4200, True], ["=HYPERLINK(\"http://evil\",\"x\")", 1.5, None]]},
            {"name": "Notes", "rows": []},
        ])
        archive = open_zip(data)
        self.assertIsNone(archive.testzip())
        for name in archive.namelist():
            ET.fromstring(archive.read(name))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        self.assertEqual([sheet.get("name") for sheet in workbook.iter(f"{S}sheet")], ["Pipeline", "Notes"])
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cells = {cell.get("r"): cell for cell in sheet.iter(f"{S}c")}
        self.assertEqual(cells["B2"].find(f"{S}v").text, "4200")
        self.assertEqual(cells["C2"].get("t"), "b")
        self.assertEqual("".join(cells["A2"].find(f"{S}is").itertext()), "Hargreaves & Co")
        formula_like = cells["A3"]
        self.assertEqual(formula_like.get("t"), "inlineStr")  # literal text
        self.assertIsNone(formula_like.find(f"{S}f"))
        self.assertNotIn("C3", {ref for ref, cell in cells.items() if cell.find(f"{S}v") is None and cell.find(f"{S}is") is None})
        self.assertNotIn(b"<f>", archive.read("xl/worksheets/sheet1.xml"))

    def test_column_letters_past_z(self):
        data = deliverables.build_xlsx([{"name": "Wide", "rows": [list(range(30))]}])
        refs = [cell.get("r") for cell in ET.fromstring(open_zip(data).read("xl/worksheets/sheet1.xml")).iter(f"{S}c")]
        self.assertEqual((refs[0], refs[25], refs[26], refs[29]), ("A1", "Z1", "AA1", "AD1"))

    def test_output_is_deterministic(self):
        build = lambda: deliverables.build_xlsx([{"name": "S", "rows": [["a", 1]]}])  # noqa: E731
        self.assertEqual(build(), build())
        self.assertEqual(deliverables.build_docx("T", [{"paragraphs": ["x"]}]), deliverables.build_docx("T", [{"paragraphs": ["x"]}]))

    def test_csv_neutralises_formulas_and_quotes_properly(self):
        data = deliverables.build_csv([["name", "note"], ["=cmd|' /C calc'!A0", "+1"], ["-2", "@SUM(A1)"], ["a,b", 'say "hi"\nnow'], ["ok", 3]]).decode()
        self.assertIn("'=cmd", data)
        self.assertIn("'+1", data)
        self.assertIn("'-2", data)
        self.assertIn("'@SUM", data)
        self.assertIn('"a,b"', data)
        self.assertEqual(data.count("\n"), 6)  # five records, one embedded newline

    def test_invalid_or_oversized_content_is_refused(self):
        bad = [
            lambda: deliverables.build_docx("", [{"paragraphs": ["x"]}]),
            lambda: deliverables.build_docx("T", []),
            lambda: deliverables.build_docx("T", [{"paragraphs": "not a list"}]),
            lambda: deliverables.build_docx("T", [{"paragraphs": [1]}]),
            lambda: deliverables.build_docx("T", [{"heading": " ", "paragraphs": []}]),
            lambda: deliverables.build_docx("T", [{"paragraphs": ["x" * 20001]}]),
            lambda: deliverables.build_xlsx([]),
            lambda: deliverables.build_xlsx([{"name": "a/b", "rows": []}]),
            lambda: deliverables.build_xlsx([{"name": "x" * 32, "rows": []}]),
            lambda: deliverables.build_xlsx([{"name": "A", "rows": []}, {"name": "a", "rows": []}]),
            lambda: deliverables.build_xlsx([{"name": "A", "rows": [[object()]]}]),
            lambda: deliverables.build_xlsx([{"name": "A", "rows": [[float("nan")]]}]),
            lambda: deliverables.build_xlsx([{"name": "A", "rows": [["x" * 32768]]}]),
            lambda: deliverables.build_xlsx([{"name": "A", "rows": [list(range(101))]}]),
            lambda: deliverables.build_xlsx([{"name": "A", "rows": [[1]] * 10001}]),
            lambda: deliverables.build_csv("rows"),
        ]
        for build in bad:
            with self.assertRaises(DError):
                build()


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "artifacts"
        self.events: list[tuple] = []

    def store(self, tenant="acme-prod", **kwargs):
        return artifacts.ArtifactStore(self.root, tenant=tenant, audit=lambda *event: self.events.append(event), **kwargs)

    def put(self, store=None, **overrides):
        values = dict(name="Pipeline summary.docx", content=b"PK-content", media_type="text/plain", agent="receptionist",
                      requested_by=ALICE, sources=SOURCES, now=T0)
        values.update(overrides)
        return (store or self.store()).put(**values)

    def test_an_artifact_records_who_asked_what_made_it_and_where_it_is(self):
        entry = self.put()
        self.assertEqual((entry["requestedBy"], entry["agent"], entry["sources"]), (ALICE, "receptionist", SOURCES))
        self.assertEqual(entry["location"], f"tier0:acme-prod/{entry['id']}")
        self.assertEqual(entry["expiresAt"], T0 + 365 * 86400)
        record, data = self.store().get(entry["id"], requested_by=ALICE, now=T0 + 1)
        self.assertEqual((data, record["sha256"]), (b"PK-content", entry["sha256"]))

    def test_the_same_file_for_the_same_requester_is_not_duplicated(self):
        first, again = self.put(), self.put(now=T0 + 5)
        self.assertEqual((first["duplicate"], again["duplicate"], again["id"]), (False, True, first["id"]))
        self.assertNotEqual(self.put(requested_by=BOB)["id"], first["id"])
        self.assertEqual(len(self.store().list()), 2)

    def test_duplicate_retries_cannot_silently_change_artifact_provenance(self):
        store = self.store(allow_restricted=True)
        original = self.put(store)
        self.assertEqual(self.put(store, now=T0 + 1)["id"], original["id"])
        variants = (
            {"agent": "assistant"},
            {"media_type": "text/markdown"},
            {"classification": "confidential"},
            {"classification": "restricted"},
            {"sources": ["kb:another#source"]},
        )
        ids = set()
        for changed in variants:
            entry = self.put(store, now=T0 + 2, **changed)
            self.assertFalse(entry["duplicate"], changed)
            self.assertNotEqual(entry["id"], original["id"])
            ids.add(entry["id"])
        self.assertEqual(len(ids), len(variants))
        self.assertEqual(len(store.list(now=T0 + 2)), 1 + len(variants))
        self.assertEqual(len([e for e in self.events if e[0] == "artifact.create"]), 1 + len(variants))

    def test_retention_hides_expired_metadata_without_requiring_manual_purge(self):
        short = self.store(retention_days=1)
        entry = self.put(short, now=T0)
        self.assertEqual([e["id"] for e in short.list(requested_by=ALICE, now=T0)], [entry["id"]])
        self.assertEqual(short.list(requested_by=ALICE, now=T0 + 86400), [])
        self.assertEqual(short.list(now=T0 + 86400), [])
        # An operator increasing retention later cannot resurrect a file whose
        # original expiry has passed.
        long = self.store(retention_days=365)
        self.assertEqual(long.list(now=T0 + 86400), [])
        self.assertTrue((self.root / "acme-prod" / entry["id"]).exists())
        self.assertEqual(short.purge_expired(now=T0 + 86400), {"expired": 1})

    def test_hostile_names_cannot_leave_the_directory_and_files_are_private(self):
        entry = self.put(name="../../etc/passwd")
        self.assertEqual(entry["name"], "etc passwd")
        path = self.root / "acme-prod" / entry["id"]
        self.assertTrue(path.is_file())
        self.assertEqual(sorted(p.name for p in (self.root / "acme-prod").iterdir() if not p.name.startswith(".")), sorted([entry["id"], "index.json"]))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "acme-prod").stat().st_mode & 0o777, 0o700)
        for bad_name in ("", " ", "...", "x" * 121, 5):
            with self.subTest(name=bad_name), self.assertRaises(Error):
                self.put(name=bad_name)

    def test_invalid_input_is_refused(self):
        for overrides in ({"media_type": "application/x-msdownload"}, {"content": b""}, {"content": "text"}, {"content": b"x" * (artifacts.MAX_BYTES + 1)},
                          {"classification": "secret"}, {"classification": "restricted"}, {"agent": "../a"}, {"requested_by": ""},
                          {"sources": ["has space"]}, {"sources": ["s"] * 21}, {"sources": "kb:x"}):
            with self.subTest(overrides=overrides), self.assertRaises(Error):
                self.put(**overrides)
        self.put(store=self.store(allow_restricted=True), classification="restricted")  # policy may allow it

    def test_only_the_requester_or_an_administrator_reads_or_deletes(self):
        entry = self.put()
        store = self.store()
        with self.assertRaises(Error):
            store.get(entry["id"], requested_by=BOB, now=T0)
        with self.assertRaises(Error):
            store.delete(entry["id"], requested_by=BOB)
        self.assertEqual(store.get(entry["id"], requested_by=ADMIN, admin=True, now=T0)[1], b"PK-content")
        self.assertEqual([item["id"] for item in store.list(requested_by=ALICE)], [entry["id"]])
        self.assertEqual(store.list(requested_by=BOB), [])
        store.delete(entry["id"], requested_by=ADMIN, admin=True)
        self.assertFalse((self.root / "acme-prod" / entry["id"]).exists())
        with self.assertRaises(Error):
            store.get(entry["id"], requested_by=ALICE, now=T0)
        with self.assertRaises(Error):
            store.delete("../../x", requested_by=ADMIN, admin=True)

    def test_a_file_changed_on_disk_is_refused(self):
        entry = self.put()
        (self.root / "acme-prod" / entry["id"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(Error, "digest"):
            self.store().get(entry["id"], requested_by=ALICE, now=T0)
        (self.root / "acme-prod" / entry["id"]).unlink()
        with self.assertRaisesRegex(Error, "missing"):
            self.store().get(entry["id"], requested_by=ALICE, now=T0)

    def test_linked_artifact_payload_is_never_read_or_deleted(self):
        entry = self.put()
        store = self.store()
        path = self.root / "acme-prod" / entry["id"]
        outside = pathlib.Path(self._tmp.name) / "unrelated-secret"
        outside.write_bytes(b"not-an-artifact")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(Error, "unsafe"):
            store.get(entry["id"], requested_by=ALICE, now=T0)
        with self.assertRaisesRegex(Error, "unsafe"):
            store.delete(entry["id"], requested_by=ALICE)
        self.assertEqual(outside.read_bytes(), b"not-an-artifact")
        path.unlink()
        os.link(outside, path)
        with self.assertRaisesRegex(Error, "unsafe"):
            store.get(entry["id"], requested_by=ALICE, now=T0)
        with self.assertRaisesRegex(Error, "unsafe"):
            store.purge_expired(now=T0 + 366 * 86400)
        self.assertTrue(outside.exists())

    def test_artifact_bytes_must_remain_private_even_with_valid_digest(self):
        entry = self.put()
        path = self.root / "acme-prod" / entry["id"]
        path.chmod(0o644)
        with self.assertRaisesRegex(Error, "unsafe"):
            self.store().get(entry["id"], requested_by=ALICE, now=T0)

    def test_index_and_lock_refuse_symlink_and_hardlink_replacement(self):
        self.put()
        store = self.store()
        index = self.root / "acme-prod" / "index.json"
        lock = self.root / "acme-prod" / ".lock"
        outside = pathlib.Path(self._tmp.name) / "outside-index"
        outside.write_bytes(index.read_bytes())
        index.unlink()
        index.symlink_to(outside)
        with self.assertRaisesRegex(Error, "unsafe"):
            store.list()
        index.unlink()
        os.link(outside, index)
        with self.assertRaisesRegex(Error, "unsafe"):
            store.list()
        index.unlink()
        index.write_bytes(outside.read_bytes())
        index.chmod(0o600)
        lock.unlink()
        lock.symlink_to(outside)
        with self.assertRaisesRegex(Error, "unsafe"):
            store.list()
        self.assertEqual(outside.read_bytes(), index.read_bytes())

    def test_tenant_directory_symlink_is_refused_before_writing(self):
        outside = pathlib.Path(self._tmp.name) / "other"
        outside.mkdir(mode=0o700)
        self.root.mkdir(mode=0o700)
        (self.root / "acme-prod").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Error, "unsafe"):
            self.put()
        self.assertEqual(list(outside.iterdir()), [])

    def test_retention_expires_and_purges_files(self):
        short = self.store(retention_days=1)
        keep = self.put(short, content=b"keep", name="keep.txt", now=T0 + 86400)
        gone = self.put(short, content=b"gone", name="gone.txt", now=T0)
        with self.assertRaisesRegex(Error, "expired"):
            short.get(gone["id"], requested_by=ALICE, now=T0 + 86400)
        self.assertEqual(short.purge_expired(now=T0 + 86400), {"expired": 1})
        self.assertFalse((self.root / "acme-prod" / gone["id"]).exists())
        self.assertEqual([item["id"] for item in short.list()], [keep["id"]])
        self.assertEqual(short.purge_expired(now=T0 + 86400), {"expired": 0})
        for days in (0, 3651, True, "30"):
            with self.subTest(days=days), self.assertRaises(Error):
                self.store(retention_days=days)

    def test_shorter_client_retention_revokes_old_files_and_purges_them(self):
        # A reviewed policy change must affect files created under the old, longer policy.
        long_policy = self.store(retention_days=365)
        entry = self.put(long_policy, now=T0)
        short_policy = self.store(retention_days=2)
        with self.assertRaisesRegex(Error, "expired"):
            short_policy.get(entry["id"], requested_by=ALICE, now=T0 + 3 * 86400)
        self.assertEqual(short_policy.purge_expired(now=T0 + 3 * 86400), {"expired": 1})
        self.assertFalse((self.root / "acme-prod" / entry["id"]).exists())
        self.assertEqual(self.store("other-client").list(), [])

    def test_retention_environment_rejects_invalid_policy_values(self):
        for value in ("0", "3651", "7.0", "true", "-1", "999999"):
            with self.subTest(value=value), self.assertRaises(Error):
                artifacts.retention_from_environ({"MODEL_GARDEN_ARTIFACT_RETENTION_DAYS": value})
        self.assertEqual(artifacts.retention_from_environ({}), 365)
        self.assertEqual(artifacts.retention_from_environ({"MODEL_GARDEN_ARTIFACT_RETENTION_DAYS": "7"}), 7)

    def test_tenants_are_isolated(self):
        entry = self.put()
        other = self.store("globex-prod")
        self.assertEqual(other.list(), [])
        with self.assertRaises(Error):
            other.get(entry["id"], requested_by=ALICE, now=T0)
        foreign_index = self.root / "globex-prod" / "index.json"
        foreign_index.write_text((self.root / "acme-prod" / "index.json").read_text())
        foreign_index.chmod(0o600)  # valid private file, but tenant identifier must still be rejected
        with self.assertRaisesRegex(Error, "another tenant"):
            other.list()
        with self.assertRaises(Error):
            artifacts.ArtifactStore(self.root, tenant="Bad Tenant")

    def test_events_are_ledgered_without_content_or_names(self):
        store = self.store()
        entry = self.put(store, name=f"{SECRET_TEXT}.txt", content=SECRET_TEXT.encode())
        store.get(entry["id"], requested_by=ALICE, now=T0)
        store.delete(entry["id"], requested_by=ALICE)
        self.assertEqual([event[0] for event in self.events], ["artifact.create", "artifact.read", "artifact.delete"])
        self.assertEqual(self.events[0][1], "agent:receptionist")
        self.assertEqual(self.events[0][2]["sources"], SOURCES)
        self.assertEqual(self.events[0][2]["location"], entry["location"])
        self.assertNotIn("Hargreaves", json.dumps(self.events))
        audit = pathlib.Path(self._tmp.name) / "audit.jsonl"
        for action, by, details in self.events:
            govern.record_operator_action(audit, action, by, details)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])


class DeliverCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple] = []
        self.store = artifacts.ArtifactStore(pathlib.Path(self._tmp.name), tenant="acme-prod", audit=lambda *event: self.events.append(event))

    def deliver(self, kind, content, **overrides):
        values = dict(kind=kind, name="Weekly pipeline", content=content, agent="account-researcher", requested_by=ALICE, sources=SOURCES, now=T0)
        values.update(overrides)
        return deliverables.deliver(self.store, **values)

    def test_a_report_is_built_stored_and_ledgered_with_its_sources(self):
        entry = self.deliver("docx", {"title": "Weekly pipeline", "sections": [{"heading": "Summary", "paragraphs": ["Three open deals."]}]})
        self.assertEqual(entry["name"], "Weekly pipeline.docx")
        self.assertEqual(entry["mediaType"], deliverables.DOCX_TYPE)
        record, data = self.store.get(entry["id"], requested_by=ALICE, now=T0)
        self.assertIn(b"Three open deals.", open_zip(data).read("word/document.xml"))
        event = self.events[0]
        self.assertEqual((event[0], event[2]["sources"], event[2]["location"]), ("artifact.create", SOURCES, entry["location"]))
        self.assertEqual(event[2]["sha256"], record["sha256"])
        self.assertNotIn("Three open deals", json.dumps(self.events))

    def test_a_spreadsheet_is_built_stored_and_ledgered(self):
        entry = self.deliver("xlsx", {"sheets": [{"name": "Pipeline", "rows": [["Customer", "Amount"], ["Hargreaves & Co", 4200]]}]}, name="pipeline.xlsx")
        self.assertEqual((entry["name"], entry["mediaType"]), ("pipeline.xlsx", deliverables.XLSX_TYPE))
        _, data = self.store.get(entry["id"], requested_by=ALICE, now=T0)
        sheet = ET.fromstring(open_zip(data).read("xl/worksheets/sheet1.xml"))
        self.assertEqual(len(list(sheet.iter(f"{S}row"))), 2)
        self.assertEqual(self.events[0][2]["mediaType"], deliverables.XLSX_TYPE)

    def test_a_csv_is_delivered_and_unsupported_or_invalid_requests_store_nothing(self):
        self.assertEqual(self.deliver("csv", {"rows": [["a", "=1"]]})["mediaType"], "text/csv")
        before = len(self.store.list())
        for kind, content in (("pptx", {}), ("pdf", {}), ("docx", {"title": "", "sections": []}), ("xlsx", "not an object"), ("docx", None)):
            with self.subTest(kind=kind), self.assertRaises(DError):
                self.deliver(kind, content)
        self.assertEqual(len(self.store.list()), before)


class OperatorCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "artifacts"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(self.root), "--tenant", "acme-prod", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_delete_and_expire_through_the_runtime_command_with_ledger_events(self):
        store = artifacts.ArtifactStore(self.root, tenant="acme-prod", retention_days=1)
        keep = store.put(name="a.txt", content=b"a", media_type="text/plain", agent="receptionist", requested_by=ALICE, now=T0)
        drop = store.put(name="b.txt", content=b"b", media_type="text/plain", agent="receptionist", requested_by=BOB, now=T0)
        code, out, _ = self.run_cli("list", "--requested-by", ALICE)
        self.assertEqual((code, [item["id"] for item in json.loads(out)]), (0, [keep["id"]]))
        self.assertNotIn("content", out)
        code, out, _ = self.run_cli("delete", drop["id"], "--by", ADMIN)
        self.assertEqual((code, json.loads(out)["deleted"]), (0, drop["id"]))
        code, out, _ = self.run_cli("--retention-days", "1", "purge-expired")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"expired": 0})  # nothing is a day old yet
        events = [json.loads(line) for line in (self.root.parent / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([event["action"] for event in events], ["artifact.delete"])
        self.assertEqual(events[0]["performedBy"], ADMIN)
        self.assertTrue(govern.verify_audit_chain(self.root.parent / "audit.jsonl")["valid"])

    def test_failures_are_clean_and_the_tenant_defaults_from_the_desired_state(self):
        code, _, err = self.run_cli("delete", "a-000000000000", "--by", ADMIN)
        self.assertEqual(code, 1)
        self.assertIn("Runtime artifacts failed", err)
        with tempfile.TemporaryDirectory() as tmp:
            desired = pathlib.Path(tmp) / "d.json"
            desired.write_text(json.dumps({"source": {"client": "Acme Dental"}}))
            self.assertEqual(cli.default_tenant(desired), "acme-dental")
            with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
                cli.default_tenant(pathlib.Path(tmp) / "missing.json")

    def test_the_operator_command_validates_before_touching_docker_and_passes_arguments(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return mock.Mock(returncode=0, stdout="gov1\n", stderr="")
            return mock.Mock(returncode=0, stdout="[]\n", stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            operator.runtime_artifacts("acme-dev", "list", requested_by=ALICE)
            operator.runtime_artifacts("acme-dev", "delete", artifact_id="a-0123456789ab", by=ADMIN)
            operator.runtime_artifacts("acme-dev", "purge-expired")
        self.assertEqual(calls[1][-3:], ["list", "--requested-by", ALICE])
        self.assertEqual(calls[3][-4:], ["delete", "a-0123456789ab", "--by", ADMIN])
        self.assertEqual(calls[5][-1], "purge-expired")
        before = len(calls)
        for kwargs in ({"command": "delete"}, {"command": "delete", "artifact_id": "nope", "by": ADMIN},
                       {"command": "delete", "artifact_id": "a-0123456789ab"}, {"command": "delete", "artifact_id": "a-0123456789ab", "by": " "},
                       {"command": "wipe"}, {"command": "list", "requested_by": "bad person"}):
            args = dict(kwargs)
            with self.assertRaises(ValueError, msg=str(kwargs)):
                operator.runtime_artifacts("acme-dev", args.pop("command"), **args)
        self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()
