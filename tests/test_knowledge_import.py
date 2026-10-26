"""Operator-assisted Tier 0 knowledge import: credential-free, draft until owner review."""
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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


IMPORT = load("knowledge_import_under_test", "scripts/import-knowledge.py")
COMPILE = load("knowledge_import_compiler", "scripts/compile-workspace.py")
KNOWLEDGE = load("knowledge_import_search", "platform/core/knowledge.py")
RULES = "# Services\nEmergency plumbing is available by phone.\n\n## Hours\nOpen Monday to Friday 9 to 5.\n\n## Escalation\nEscalate dangerous leaks to a person.\n"


class KnowledgeImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.workspace = self.root / "client-a"
        shutil.copytree(ROOT / "examples" / "workspace", self.workspace)
        self.source = self.root / "upload.md"
        self.source.write_text(RULES, encoding="utf-8")
        self.kwargs = dict(name="service-guide", owner="business-owner", agent="receptionist", classification="internal")

    def import_file(self, **overrides):
        return IMPORT.import_knowledge(self.workspace, self.source, **{**self.kwargs, **overrides})

    def test_draft_is_selected_but_cannot_ground_answers_until_owner_approves(self):
        result = self.import_file()
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["sha256"], KNOWLEDGE.digest(RULES))
        self.assertEqual(result["sectionCount"], 3)
        self.assertNotIn(RULES, json.dumps(result))
        knowledge_path = self.workspace / "knowledge" / "service-guide.yaml"
        document = yaml.safe_load(knowledge_path.read_text())
        self.assertEqual(document["spec"]["file"], "service-guide.md")
        self.assertEqual(document["spec"]["audiences"], ["receptionist"])
        self.assertEqual(document["metadata"]["owner"], "business-owner")
        agent_path = self.workspace / "agents/front-office/receptionist/agent.yaml"
        self.assertIn("service-guide", yaml.safe_load(agent_path.read_text())["spec"]["knowledge"])
        compiled = COMPILE.compile_workspace(self.workspace, "receptionist")[0]
        self.assertEqual(
            KNOWLEDGE.search_selected(compiled, "emergency plumbing", source="service-guide")["reason"],
            "source-draft",
        )
        document["spec"]["status"] = "approved"  # reviewed client-workspace change, not the importer
        knowledge_path.write_text(yaml.safe_dump(document), encoding="utf-8")
        approved = COMPILE.compile_workspace(self.workspace, "receptionist")[0]
        result = KNOWLEDGE.search_selected(approved, "emergency plumbing", source="service-guide", today=dt.date(2026, 10, 26))
        self.assertTrue(result["documents"])
        self.assertTrue(result["documents"][0]["citation"].startswith("knowledge:service-guide#"))

    def test_dry_run_has_no_side_effects(self):
        before = (self.workspace / "agents/front-office/receptionist/agent.yaml").read_bytes()
        result = self.import_file(dry_run=True)
        self.assertEqual(result["status"], "draft")
        self.assertTrue(result["dryRun"])
        self.assertFalse((self.workspace / "knowledge/service-guide.yaml").exists())
        self.assertFalse((self.workspace / "knowledge/service-guide.md").exists())
        self.assertEqual(before, (self.workspace / "agents/front-office/receptionist/agent.yaml").read_bytes())

    def test_repeat_is_idempotent_and_changed_content_cannot_overwrite(self):
        self.import_file()
        agent = self.workspace / "agents/front-office/receptionist/agent.yaml"
        before = agent.read_bytes()
        self.assertTrue(self.import_file()["alreadyImported"])
        self.assertEqual(before, agent.read_bytes())
        self.source.write_text("# Changed\nNever overwrite an approved document.\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.import_file()
        self.assertEqual((self.workspace / "knowledge/service-guide.md").read_text(), RULES)
        self.assertEqual(before, agent.read_bytes())

    def test_invalid_input_or_identity_cannot_create_a_source(self):
        for overrides in ({"name": "../escape"}, {"name": "bad name"}, {"owner": ""}, {"agent": "missing"}, {"classification": "restricted"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.import_file(**overrides)
        self.source.write_bytes(b"\xff\xfe")
        with self.assertRaises(ValueError):
            self.import_file()
        self.source.write_text(RULES, encoding="utf-8")
        symlink = self.root / "link.md"
        symlink.symlink_to(self.source)
        with self.assertRaises(ValueError):
            IMPORT.import_knowledge(self.workspace, symlink, **self.kwargs)
        self.assertFalse((self.workspace / "knowledge/service-guide.yaml").exists())

    def test_symlinked_destination_or_agent_is_rejected(self):
        knowledge = self.workspace / "knowledge"
        renamed = self.workspace / "knowledge-real"
        knowledge.rename(renamed)
        knowledge.symlink_to(renamed, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.import_file()
        knowledge.unlink()
        renamed.rename(knowledge)
        agent = self.workspace / "agents/front-office/receptionist/agent.yaml"
        outside = self.root / "outside.yaml"
        outside.write_bytes(agent.read_bytes())
        agent.unlink()
        agent.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.import_file()
        self.assertFalse((self.workspace / "knowledge/service-guide.yaml").exists())
        self.assertEqual(outside.read_bytes(), (self.root / "outside.yaml").read_bytes())

    def test_two_client_workspaces_do_not_share_imported_data(self):
        other = self.root / "client-b"
        shutil.copytree(ROOT / "examples" / "workspace", other)
        before = (other / "agents/front-office/receptionist/agent.yaml").read_bytes()
        self.import_file()
        self.assertFalse((other / "knowledge/service-guide.yaml").exists())
        self.assertEqual(before, (other / "agents/front-office/receptionist/agent.yaml").read_bytes())


if __name__ == "__main__":
    unittest.main()
