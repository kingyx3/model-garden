"""Credential-free checks for Tier 0 files.* and docs.* over the artifact store."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


files = _load("tier0_files_under_test", "platform/core/builtin_files.py")
artifacts = files._artifacts()
deliverables = _load("deliverables_for_tier0", "platform/core/deliverables.py")
Error = files.FilesError
CATALOG = json.loads((ROOT / "contracts/v1/capabilities.json").read_text())["families"]
ALICE, BOB = "alice@client.example", "bob@client.example"
T0 = 1_800_000_000


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple] = []
        self.store = artifacts.ArtifactStore(pathlib.Path(self._tmp.name), tenant="acme-prod", audit=lambda *event: self.events.append(event))

    def files(self, who=ALICE):
        return files.Tier0Files(self.store, agent="receptionist", requested_by=who)

    def test_docs_create_stores_a_document_and_returns_its_location(self):
        result = self.files().docs_create({"title": "Weekly pipeline", "content": "# Pipeline\n\nThree open deals."}, now=T0)
        self.assertTrue(result["documentId"].startswith("a-"))
        self.assertEqual(result["externalObjectRef"], f"tier0:acme-prod/{result['documentId']}")
        record, data = self.store.get(result["documentId"], requested_by=ALICE, now=T0)
        self.assertEqual((record["name"], record["mediaType"], data), ("Weekly pipeline.md", "text/markdown", b"# Pipeline\n\nThree open deals."))
        self.assertEqual(self.events[0][0], "artifact.create")  # ledgered by the store with location, digest and size
        self.assertNotIn("Three open deals", json.dumps(self.events))
        self.assertNotIn("Weekly pipeline", json.dumps(self.events))

    def test_a_document_round_trips_through_docs_read_and_files_read(self):
        created = self.files().docs_create({"title": "Notes", "content": "Remember the Friday slot."}, now=T0)
        document = self.files().docs_read({"documentId": created["documentId"]}, now=T0 + 1)["document"]
        read = self.files().files_read({"fileId": created["documentId"]}, now=T0 + 1)["file"]
        self.assertEqual((document["text"], read["text"]), ("Remember the Friday slot.", "Remember the Friday slot."))
        self.assertFalse(document["truncated"])

    def test_files_write_and_the_no_folder_rule(self):
        written = self.files().files_write({"name": "hours.txt", "content": "9 to 5"}, now=T0)
        self.assertEqual(self.store.get(written["fileId"], requested_by=ALICE, now=T0)[0]["mediaType"], "text/plain")
        with self.assertRaisesRegex(Error, "no folders"):
            self.files().files_write({"name": "x.md", "content": "y", "folderId": "root"}, now=T0)

    def test_a_requester_searches_and_reads_only_their_own_files(self):
        mine = self.files().docs_create({"title": "Alice pipeline", "content": "mine"}, now=T0)
        theirs = self.files(BOB).docs_create({"title": "Bob pipeline", "content": "his"}, now=T0)
        found = self.files().files_search({"query": "pipeline"})["files"]
        self.assertEqual([item["fileId"] for item in found], [mine["documentId"]])
        self.assertEqual(self.files(BOB).files_search({"query": "PIPELINE"})["files"][0]["fileId"], theirs["documentId"])  # case-insensitive
        with self.assertRaisesRegex(Error, "only the person who asked"):
            self.files().files_read({"fileId": theirs["documentId"]}, now=T0)
        with self.assertRaises(Error):
            self.files().docs_read({"documentId": theirs["documentId"]}, now=T0)

    def test_files_search_excludes_expired_files_even_without_purge(self):
        short = artifacts.ArtifactStore(pathlib.Path(self._tmp.name), tenant="acme-prod",
                                        retention_days=1, audit=lambda *event: self.events.append(event))
        requester = files.Tier0Files(short, agent="receptionist", requested_by=ALICE)
        created = requester.files_write({"name": "private.txt", "content": "sensitive"}, now=T0)
        self.assertEqual(len(requester.executors(now=lambda: T0)["files.search"]({"query": "private"})["files"]), 1)
        self.assertEqual(requester.executors(now=lambda: T0 + 86400)["files.search"]({"query": "private"})["files"], [])
        with self.assertRaisesRegex(artifacts.ArtifactError, "expired"):
            short.get(created["fileId"], requested_by=ALICE, now=T0 + 86400)

    def test_binary_deliverables_show_as_metadata_only(self):
        entry = deliverables.deliver(self.store, kind="xlsx", name="pipeline", content={"sheets": [{"name": "S", "rows": [["a", 1]]}]},
                                     agent="receptionist", requested_by=ALICE, now=T0)
        listed = self.files().files_search({"query": "pipeline"})["files"][0]
        self.assertEqual(listed["fileId"], entry["id"])
        read = self.files().files_read({"fileId": entry["id"]}, now=T0)["file"]
        self.assertIsNone(read["text"])
        self.assertIn("binary file", read["note"])
        self.assertNotIn("PK", json.dumps(read))

    def test_long_text_is_truncated_for_the_model(self):
        created = self.files().docs_create({"title": "Long", "content": "x" * 30_000}, now=T0)
        read = self.files().docs_read({"documentId": created["documentId"]}, now=T0)["document"]
        self.assertEqual((len(read["text"]), read["truncated"]), (files.MAX_READ_TEXT, True))

    def test_invalid_requests_are_refused_and_extra_arguments_are_not_ignored(self):
        client = self.files()
        for call, arguments in (
            (client.docs_create, {}), (client.docs_create, {"title": "T"}), (client.docs_create, {"title": "T", "content": " "}),
            (client.docs_create, {"title": "T" * 121, "content": "c"}), (client.docs_create, {"title": "T", "content": "c", "folderId": "x"}),
            (client.docs_create, {"title": "T", "content": "c" * 200_001}), (client.files_write, {"name": "", "content": "c"}),
            (client.files_search, {}), (client.files_search, {"query": "q", "requestedBy": BOB}), (client.files_search, {"query": "q" * 101}),
            (client.files_read, {"fileId": "../x"}), (client.files_read, {"fileId": "a-000000000000", "requestedBy": BOB}),
            (client.docs_read, {"documentId": 5}), (client.docs_create, "not an object"),
        ):
            with self.subTest(call=call.__name__, arguments=str(arguments)[:50]), self.assertRaises(Error):
                call(arguments)
        with self.assertRaisesRegex(Error, "no such artifact"):
            client.files_read({"fileId": "a-000000000000"})
        self.assertEqual(self.store.list(), [])

    def test_the_executors_cover_exactly_the_portable_tier0_tools_and_are_bound_to_one_requester(self):
        tools = files_tools = sorted(self.files().executors())
        self.assertEqual(tools, ["docs.create", "docs.read", "files.read", "files.search", "files.write"])
        for name in tools:
            family = name.split(".")[0]
            self.assertIn(name, CATALOG[family]["tools"])
        executors = self.files().executors(now=lambda: T0)
        created = executors["docs.create"]({"title": "Bound", "content": "c"})
        self.assertEqual(self.store.list()[0]["requestedBy"], ALICE)
        self.assertEqual(executors["docs.read"]({"documentId": created["documentId"]})["document"]["text"], "c")
        with self.assertRaises(Error):
            self.files(BOB).executors(now=lambda: T0)["docs.read"]({"documentId": created["documentId"]})
        self.assertEqual(files_tools, tools)


if __name__ == "__main__":
    unittest.main()
