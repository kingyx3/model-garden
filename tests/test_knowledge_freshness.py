"""Credential-free checks for knowledge freshness: an approved source past its reviewBy date is stale and fails closed."""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


knowledge = load("knowledge_for_freshness", "platform/core/knowledge.py")
capabilities = load("capabilities_for_freshness", "platform/core/capabilities.py")
compiler = load("compiler_for_freshness", "scripts/compile-workspace.py")

BINDING = {"environment": "dev", "connectors": {
    "kb": {"provider": "text-knowledge", "tier": 0},
    "crm": {"provider": "built-in-crm", "tier": 0},
    "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://google/dev"},
}}
TODAY = dt.date(2026, 6, 1)
TEXT = "# Hours\n\nWe open from 9am to 5pm on weekdays.\n"


def index(**spec) -> dict:
    return knowledge.build_index("rules", TEXT, filename="rules.md", spec={"status": "approved", **spec})


class AvailabilityTests(unittest.TestCase):
    def reason(self, **spec):
        return knowledge.availability(index(**spec), agent="receptionist", today=TODAY)

    def test_the_review_date_is_the_last_usable_day(self):
        self.assertIsNone(self.reason(reviewBy="2026-06-01"))
        self.assertIsNone(self.reason(reviewBy="2027-01-01"))
        self.assertEqual(self.reason(reviewBy="2026-05-31"), "source-stale")

    def test_no_review_date_never_goes_stale_and_an_impossible_date_fails_closed(self):
        self.assertIsNone(self.reason())
        self.assertEqual(self.reason(reviewBy="2026-02-30"), "source-review-date-invalid")

    def test_a_stale_source_does_not_answer_and_names_why(self):
        outcome = knowledge.search(index(reviewBy="2026-01-01"), "opening hours", agent="receptionist", today=TODAY)
        self.assertEqual((outcome["documents"], outcome["reason"]), ([], "source-stale"))
        fresh = knowledge.search(index(reviewBy="2027-01-01"), "opening hours", agent="receptionist", today=TODAY)
        self.assertTrue(fresh["documents"])

    def test_declaration_state_agrees_with_what_retrieval_does(self):
        for spec in ({}, {"reviewBy": "2026-06-01"}, {"reviewBy": "2026-05-31"}, {"reviewBy": "bad"}):
            state = knowledge.source_state({"type": "text-file", "status": "approved", **spec}, agent="receptionist", today=TODAY)
            reason = knowledge.availability(index(**spec), agent="receptionist", today=TODAY)
            self.assertEqual(state, "usable" if reason is None else reason.removeprefix("source-"), spec)


class WorkspaceTests(unittest.TestCase):
    def workspace(self, root: pathlib.Path, **changes) -> pathlib.Path:
        target = root / "client"
        shutil.copytree(ROOT / "examples/workspace", target)
        path = target / "knowledge/rules-of-engagement.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["spec"].update(changes)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return target

    def test_the_schema_accepts_a_review_date_and_rejects_a_malformed_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            compiled = compiler.compile_workspace(self.workspace(pathlib.Path(tmp), reviewBy="2999-12-31"), "receptionist",
                                                  environment_binding=BINDING)[0]
        source = next(s for s in compiled["knowledge"] if s["metadata"]["name"] == "rules-of-engagement")
        self.assertEqual(source["spec"]["reviewBy"], "2999-12-31")
        for bad in ("next year", "2026-6-1", 20261231):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError, msg=str(bad)):
                compiler.compile_workspace(self.workspace(pathlib.Path(tmp), reviewBy=bad), "receptionist", environment_binding=BINDING)

    def test_a_non_text_source_cannot_carry_a_review_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "client"
            shutil.copytree(ROOT / "examples/workspace", workspace)
            path = workspace / "knowledge/business-information.yaml"
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["spec"]["reviewBy"] = "2999-12-31"
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)

    def test_a_stale_source_blocks_the_kb_skill_with_a_review_action_and_stops_runtime_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), reviewBy="2020-01-01")
            report = compiler.workspace_readiness(workspace, "receptionist", (), BINDING)
            faq = next(s for s in report[0]["skills"] if s["name"] == "answer-faq")
            self.assertFalse(faq["ready"])
            self.assertEqual(faq["dependencies"][0]["status"], "no-content")
            self.assertIn("rules-of-engagement stale", compiler.format_readiness(report))
            self.assertIn("set a later reviewBy date", faq["remediation"])
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            answer = knowledge.search_selected(desired, "What are your opening hours?", today=dt.date(2026, 10, 15))
        self.assertEqual(answer["documents"], [])
        self.assertEqual(answer["reason"], "source-stale")

    def test_a_current_review_date_keeps_everything_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), reviewBy="2999-12-31")
            report = compiler.workspace_readiness(workspace, "receptionist", (), BINDING)
            self.assertTrue(next(s for s in report[0]["skills"] if s["name"] == "answer-faq")["ready"])
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            self.assertTrue(knowledge.search_selected(desired, "What are your opening hours?", today=dt.date(2026, 10, 15))["documents"])

    def test_the_reference_workspace_does_not_carry_a_date_that_could_expire_under_the_test_suite(self):
        source = yaml.safe_load((ROOT / "examples/workspace/knowledge/rules-of-engagement.yaml").read_text())
        self.assertNotIn("reviewBy", source["spec"])


if __name__ == "__main__":
    unittest.main()
