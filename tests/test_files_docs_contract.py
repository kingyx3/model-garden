"""One conformance suite for the portable files.* and docs.* operations, run against the Tier 0 route (#91, #101).

An upstream MCP, managed or self-hosted mapping of ``docs.create``, ``docs.read``, ``files.search``, ``files.read`` and
``files.write`` is held to the same contract as the built-in fallback by subclassing ``PortableFileOperations`` and
implementing ``backend`` (and ``who_can_see`` if it supports something Tier 0 does not). Nothing here talks to a provider.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builtin_files = _load("builtin_files_for_contract", "platform/core/builtin_files.py")
FAMILIES = json.loads((ROOT / "contracts/v1/capabilities.json").read_text())["families"]
TOOLS = {name: tool for family in ("docs", "files") for name, tool in FAMILIES[family]["tools"].items()}
OPERATIONS = ("docs.create", "docs.read", "files.search", "files.read", "files.write")
ALICE, BOB = "alice@client.example", "bob@client.example"
REPORT = {"sections": [{"heading": "Summary", "paragraphs": ["Hours are 9 to 5."]}]}


def schema_errors(schema: dict[str, Any], value: Any, where: str = "value") -> list[str]:
    """The small JSON Schema subset the capability catalogue uses: type, enum, required, properties and array items."""
    if "enum" in schema:
        return [] if value in schema["enum"] else [f"{where} must be one of {schema['enum']}"]
    kind = schema.get("type")
    checks = {"object": dict, "string": str, "array": list, "integer": int, "boolean": bool}
    if kind in checks and (not isinstance(value, checks[kind]) or (kind == "integer" and isinstance(value, bool))):
        return [f"{where} must be {kind}"]
    errors: list[str] = []
    if kind == "object":
        errors += [f"{where}.{key} is required" for key in schema.get("required", []) if key not in value]
        for key, item in value.items():
            if key in schema.get("properties", {}):
                errors += schema_errors(schema["properties"][key], item, f"{where}.{key}")
    if kind == "array" and "items" in schema:
        for index, item in enumerate(value):
            errors += schema_errors(schema["items"], item, f"{where}[{index}]")
    return errors


class PortableFileOperations:
    """Mixin: behaviour every route for the portable file operations must show. Subclass with ``unittest.TestCase``."""

    supports_folders = False

    def backend(self, who: str) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        raise NotImplementedError

    def call(self, tool: str, arguments: dict[str, Any], who: str = ALICE) -> dict[str, Any]:
        self.assertEqual(schema_errors(TOOLS[tool]["arguments"], arguments), [], f"{tool} arguments are outside the contract")
        result = self.backend(who)[tool](arguments)
        self.assertEqual(schema_errors(TOOLS[tool]["result"], result), [], f"{tool} result is outside the contract")
        return result

    # -- the contract itself ------------------------------------------------------------------

    def test_every_operation_is_implemented_and_its_ledger_fields_are_arguments(self):
        self.assertEqual(sorted(set(OPERATIONS) - set(self.backend(ALICE))), [])
        for name in OPERATIONS:
            tool = TOOLS[name]
            self.assertLessEqual(set(tool["ledgerFields"]), set(tool["arguments"]["properties"]), name)
            self.assertNotIn("content", tool["ledgerFields"], name)
            self.assertNotIn("structure", tool["ledgerFields"], name)

    def test_unknown_and_missing_arguments_are_refused(self):
        for name, arguments in (("docs.create", {"title": "T", "content": "x", "owner": "bob"}), ("docs.create", {"content": "x"}),
                                ("files.write", {"name": "n.md"}), ("files.search", {}), ("files.read", {}), ("docs.read", {})):
            with self.subTest(name=name, arguments=sorted(arguments)):
                with self.assertRaises(ValueError):
                    self.backend(ALICE)[name](arguments)

    # -- documents ----------------------------------------------------------------------------

    def test_docs_create_markdown_returns_a_location_and_is_idempotent(self):
        first = self.call("docs.create", {"title": "Notes", "content": "# Notes\n\nOK"})
        again = self.call("docs.create", {"title": "Notes", "content": "# Notes\n\nOK"})
        self.assertEqual(first["documentId"], again["documentId"])
        self.assertTrue(first["externalObjectRef"], "a created file is ledgered with where it is")

    def test_docs_create_builds_each_real_format_or_refuses_it(self):
        structures = {"docx": REPORT, "pdf": REPORT, "pptx": {"slides": [{"title": "Plan", "bullets": ["One"]}]},
                      "xlsx": {"sheets": [{"name": "S", "rows": [["a", 1]]}]}, "csv": {"rows": [["a", "b"]]}}
        for kind, structure in structures.items():
            with self.subTest(kind):
                created = self.call("docs.create", {"title": f"Deck {kind}", "format": kind, "structure": structure,
                                                    "sources": ["kb:rules-of-engagement#hours"]})
                self.assertTrue(created["externalObjectRef"])
        for bad in ({"format": "exe", "structure": REPORT}, {"format": "docx", "structure": {"slides": []}},
                    {"format": "markdown", "content": "x", "structure": REPORT}, {"format": "docx"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.backend(ALICE)["docs.create"]({"title": "T", **bad})

    def test_text_is_not_silently_substituted_for_a_binary_format(self):
        with self.assertRaises(ValueError):
            self.backend(ALICE)["docs.create"]({"title": "T", "format": "xlsx", "content": "a,b"})

    def test_docs_read_returns_what_docs_create_stored_for_the_same_person_only(self):
        created = self.call("docs.create", {"title": "Notes", "content": "Remember Friday."})
        document = self.call("docs.read", {"documentId": created["documentId"]})["document"]
        self.assertEqual(document["text"], "Remember Friday.")
        with self.assertRaises(ValueError):
            self.backend(BOB)["docs.read"]({"documentId": created["documentId"]})

    # -- files --------------------------------------------------------------------------------

    def test_files_write_search_read_round_trip(self):
        written = self.call("files.write", {"name": "hours.txt", "content": "9 to 5"})
        self.assertTrue(written["fileId"] and written["externalObjectRef"])
        found = self.call("files.search", {"query": "hours"})["files"]
        self.assertEqual([item["fileId"] for item in found], [written["fileId"]])
        self.assertNotIn("content", json.dumps(found), "search results hold names and ids, never contents")
        self.assertEqual(self.call("files.read", {"fileId": written["fileId"]})["file"]["text"], "9 to 5")

    def test_a_person_sees_only_their_own_files(self):
        written = self.call("files.write", {"name": "private.txt", "content": "secret plan"})
        self.assertEqual(self.call("files.search", {"query": "private"}, who=BOB)["files"], [])
        with self.assertRaises(ValueError):
            self.backend(BOB)["files.read"]({"fileId": written["fileId"]})

    def test_a_folder_the_route_cannot_honour_is_refused_not_ignored(self):
        if self.supports_folders:
            self.skipTest("this route supports folders")
        with self.assertRaises(ValueError):
            self.backend(ALICE)["files.write"]({"name": "n.txt", "content": "x", "folderId": "F1"})

    def test_binary_files_are_metadata_only_to_the_model(self):
        created = self.call("docs.create", {"title": "Sheet", "format": "xlsx", "structure": {"sheets": [{"name": "S", "rows": [["a"]]}]}})
        read = self.call("files.read", {"fileId": created["documentId"]})
        self.assertIsNone(read["file"]["text"])


class Tier0Conformance(PortableFileOperations, unittest.TestCase):
    """The built-in fallback used when no file system is bound."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        artifacts = builtin_files._artifacts()
        self.store = artifacts.ArtifactStore(pathlib.Path(self._tmp.name), tenant="acme-prod")

    def backend(self, who: str):
        return builtin_files.Tier0Files(self.store, agent="receptionist", requested_by=who).executors(now=lambda: 1_800_000_000)


if __name__ == "__main__":
    unittest.main()
