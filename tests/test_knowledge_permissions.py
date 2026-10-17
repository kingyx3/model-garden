"""Credential-free checks for who a knowledge source may answer, its language, and the freshness it reports (#116, #111)."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


K = _load("knowledge_for_permissions", "platform/core/knowledge.py")
kb_runtime = _load("kb_runtime_cases_for_permissions", "tests/test_kb_runtime.py")
docs_runtime = _load("docs_runtime_cases_for_permissions", "tests/test_docs_runtime.py")
compiler = kb_runtime.compiler
capabilities = _load("capabilities_for_permissions", "platform/core/capabilities.py")

TODAY = dt.date(2026, 6, 1)
TEXT = "# Opening hours\nWe are open 9am to 5pm Monday to Friday.\n\n# Staff discount\nTeam members get 20 percent off.\n"


def index(**spec):
    return K.build_index("rules", TEXT, filename="rules.md", spec={"status": "approved", **spec})


class AccessTests(unittest.TestCase):
    def search(self, access, **spec):
        return K.search(index(**spec), "opening hours", agent="receptionist", today=TODAY, access=access)

    def test_public_and_internal_answer_everyone_and_an_unlabelled_source_is_internal(self):
        for spec in ({}, {"classification": "public"}, {"classification": "internal"}):
            for access in ("public", "member"):
                self.assertTrue(self.search(access, **spec)["documents"], (spec, access))
        self.assertEqual(index()["classification"], "internal")

    def test_confidential_answers_a_verified_member_and_never_a_public_or_unknown_asker(self):
        self.assertTrue(self.search("member", classification="confidential")["documents"])
        for access in ("public", "anything-else", "", None):
            result = self.search(access, classification="confidential")
            self.assertEqual((result["documents"], result["reason"]), ([], "source-members-only"), access)

    def test_restricted_is_not_served_by_the_built_in_store_to_anyone(self):
        for access in ("public", "member"):
            result = self.search(access, classification="restricted")
            self.assertEqual((result["documents"], result["reason"]), ([], "source-restricted"))

    def test_a_missing_access_argument_means_public(self):
        result = K.search(index(classification="confidential"), "opening hours", agent="receptionist", today=TODAY)
        self.assertEqual(result["reason"], "source-members-only")

    def test_readiness_state_agrees_with_what_retrieval_does_for_a_member(self):
        def state(**spec):
            return K.source_state({"type": "text-file", "status": "approved", **spec}, agent="receptionist", today=TODAY)
        self.assertEqual(state(classification="confidential"), "usable")
        self.assertEqual(state(classification="restricted"), "restricted")
        self.assertEqual(state(), "usable")
        for classification in K.CLASSIFICATIONS:
            reason = K.availability(index(classification=classification), agent="receptionist", today=TODAY, access="member")
            self.assertEqual(state(classification=classification), "usable" if reason is None else reason.removeprefix("source-"))

    def test_the_upload_check_warns_when_a_source_cannot_answer_customers(self):
        self.assertEqual([w for w in K.check_source(TEXT, filename="r.md")["warnings"] if "classified" in w], [])
        confidential = K.check_source(TEXT, filename="r.md", classification="confidential")["warnings"]
        self.assertTrue(any("only a verified team member" in warning for warning in confidential))
        restricted = K.check_source(TEXT, filename="r.md", classification="restricted")["warnings"]
        self.assertTrue(any("does not serve it at all" in warning for warning in restricted))


class FreshnessTests(unittest.TestCase):
    def documents(self, **spec):
        return K.search(index(**spec), "opening hours", agent="receptionist", today=TODAY)["documents"]

    def test_each_section_reports_how_current_its_basis_is(self):
        (first, *_) = self.documents(effectiveDate="2026-01-01", reviewBy="2026-12-31")
        self.assertEqual(first["freshness"], {"effectiveDate": "2026-01-01", "reviewBy": "2026-12-31", "reviewInDays": 213})
        self.assertNotIn("reviewSoon", first["freshness"])

    def test_a_review_due_within_two_weeks_is_flagged(self):
        (first, *_) = self.documents(reviewBy="2026-06-15")
        self.assertEqual((first["freshness"]["reviewInDays"], first["freshness"]["reviewSoon"]), (14, True))
        (later, *_) = self.documents(reviewBy="2026-06-16")
        self.assertNotIn("reviewSoon", later["freshness"])

    def test_the_last_day_still_answers_and_the_next_does_not(self):
        (last, *_) = self.documents(reviewBy="2026-06-01")
        self.assertEqual(last["freshness"], {"reviewBy": "2026-06-01", "reviewInDays": 0, "reviewSoon": True})
        stale = K.search(index(reviewBy="2026-05-31"), "opening hours", agent="receptionist", today=TODAY)
        self.assertEqual((stale["documents"], stale["reason"]), ([], "source-stale"))

    def test_no_dates_means_no_freshness_claim(self):
        (first, *_) = self.documents()
        self.assertNotIn("freshness", first)


class LocaleTests(unittest.TestCase):
    def state(self, *specs):
        sources = [{"metadata": {"name": name}, "spec": {"type": "text-file", "classification": "internal", "file": "x.md", "status": "approved", **spec}}
                   for name, spec in specs]
        return {"source": {"name": "receptionist"}, "knowledge": sources,
                "knowledgeContent": {name: {"content": TEXT, "source": "x.md"} for name, _ in specs}}

    def test_a_request_for_one_language_skips_sources_labelled_with_another(self):
        state = self.state(("english", {"locale": "en-SG"}), ("chinese", {"locale": "zh"}), ("neutral", {}))
        names = lambda **kw: sorted({d["citation"].split("#")[0] for d in K.search_selected(state, "opening hours", today=TODAY, limit=10, **kw)["documents"]})
        self.assertEqual(names(), ["knowledge:chinese", "knowledge:english", "knowledge:neutral"])
        self.assertEqual(names(locale="en"), ["knowledge:english", "knowledge:neutral"])
        self.assertEqual(names(locale="EN-GB"), ["knowledge:english", "knowledge:neutral"])  # the language, not the region, decides
        self.assertEqual(names(locale="zh"), ["knowledge:chinese", "knowledge:neutral"])

    def test_a_language_with_no_source_says_why(self):
        state = self.state(("english", {"locale": "en"}))
        result = K.search_selected(state, "opening hours", today=TODAY, locale="fr")
        self.assertEqual((result["documents"], result["reason"], result["sources"]), ([], "source-other-locale", {"english": "source-other-locale"}))

    def test_returned_sections_name_their_language(self):
        (first, *_) = K.search_selected(self.state(("english", {"locale": "en-SG"})), "opening hours", today=TODAY)["documents"]
        self.assertEqual(first["locale"], "en-SG")

    def test_a_bad_language_tag_is_refused(self):
        for bad in ("", "english", "en_SG", "e", "en-", "x" * 40, 5, "en SG"):
            with self.assertRaises(K.KnowledgeError, msg=str(bad)):
                K.search_selected(self.state(("a", {})), "opening hours", today=TODAY, locale=bad)


class RuntimeTests(kb_runtime.RuntimeKbSearchTests):
    """The sidecar decides the asker's standing from its own principal, never from an argument."""

    def asker(self, user, **source_spec):
        principal = docs_runtime.principal_for(user)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        desired = compiler.compile_workspace(kb_runtime.workspace(str(root), **source_spec), "receptionist")[0]
        desired["source"]["client"] = "acme"
        path = root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        manifest = kb_runtime.runtime_module.CAPABILITIES.build_manifest(desired, kb_runtime.KB_BINDING)
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return kb_runtime.runtime_module.GovernedToolRuntime(
            path, root / "audit.jsonl", root / "approvals", environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(root / "manifest.json"),
                                                                    "MODEL_GARDEN_INITIATING_IDENTITY": "call:trace-5"},
            principal_resolver=(lambda: principal) if principal is not None else None)

    def test_a_confidential_source_answers_a_mapped_member_but_not_a_stranger_or_an_unattributed_call(self):
        member = self.asker("U-ALICE", classification="confidential").kb_search(query="opening hours")["result"]
        self.assertTrue(member["documents"])
        stranger = self.asker("U-STRANGER", classification="confidential").kb_search(query="opening hours")["result"]
        self.assertEqual((stranger["documents"], stranger["reason"]), ([], "source-members-only"))
        with tempfile.TemporaryDirectory() as tmp:
            unattributed = self.runtime(pathlib.Path(tmp), classification="confidential").kb_search(query="opening hours")["result"]
        self.assertEqual((unattributed["documents"], unattributed["reason"]), ([], "source-members-only"))

    def test_a_restricted_source_answers_nobody_even_a_member(self):
        result = self.asker("U-ALICE", classification="restricted").kb_search(query="opening hours")["result"]
        self.assertEqual((result["documents"], result["reason"]), ([], "source-restricted"))

    def test_the_default_label_still_answers_a_stranger_so_existing_workspaces_are_unchanged(self):
        result = self.asker("U-STRANGER").kb_search(query="opening hours")["result"]
        self.assertTrue(result["documents"])

    def test_there_is_no_argument_that_asks_for_more_access(self):
        import inspect
        parameters = set(inspect.signature(kb_runtime.runtime_module.GovernedToolRuntime.kb_search).parameters)
        self.assertEqual(parameters, {"self", "query", "source", "limit", "justification", "locale"})

    def test_locale_is_validated_and_bound_into_the_exact_call(self):
        runtime = self.asker("U-ALICE", locale="en")
        for bad in ("english", "", "en_SG"):
            with self.assertRaises(ValueError, msg=bad):
                runtime.kb_search(query="opening hours", locale=bad)
        self.assertTrue(runtime.kb_search(query="opening hours", locale="en")["result"]["documents"])
        none = runtime.kb_search(query="opening hours", locale="fr")["result"]
        self.assertEqual((none["documents"], none["reason"]), ([], "source-other-locale"))


class ReadinessTests(unittest.TestCase):
    BINDING = {"environment": "dev", "connectors": {"kb": {"provider": "text-knowledge", "tier": 0}, "crm": {"provider": "built-in-crm", "tier": 0}}}

    def faq(self, **spec):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "client"
            shutil.copytree(ROOT / "examples/workspace", workspace)
            path = workspace / "knowledge/rules-of-engagement.yaml"
            document = yaml.safe_load(path.read_text())
            document["spec"].update(spec)
            path.write_text(yaml.safe_dump(document, sort_keys=False))
            report = compiler.workspace_readiness(workspace, "receptionist", (), self.BINDING)
        return next(skill for skill in report[0]["skills"] if skill["name"] == "answer-faq")

    def test_a_restricted_only_source_blocks_the_skill_with_one_plain_remediation(self):
        faq = self.faq(classification="restricted")
        self.assertFalse(faq["ready"])
        self.assertIn("classified restricted", faq["remediation"])
        self.assertIn("document-level permissions", faq["remediation"])

    def test_a_confidential_or_default_source_leaves_the_skill_ready(self):
        self.assertTrue(self.faq(classification="confidential")["ready"])
        self.assertTrue(self.faq()["ready"])


class SchemaTests(unittest.TestCase):
    def test_locale_is_declared_for_text_files_only(self):
        schema = json.loads((ROOT / "contracts/v1/knowledge-source.schema.json").read_text())
        self.assertIn("locale", schema["properties"]["spec"]["properties"])
        refused = schema["properties"]["spec"]["allOf"][0]["else"]["not"]["anyOf"]
        self.assertIn({"required": ["locale"]}, refused)

    def test_the_catalogue_lets_kb_search_carry_a_locale(self):
        tool = json.loads((ROOT / "contracts/v1/capabilities.json").read_text())["families"]["kb"]["tools"]["kb.search"]
        self.assertIn("locale", tool["arguments"]["properties"])
        self.assertEqual(tool["arguments"]["required"], ["query"])


if __name__ == "__main__":
    unittest.main()
