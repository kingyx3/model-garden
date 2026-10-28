"""Reviewed Tier 0 upload -> private draft import; never a live/approved source."""
import hashlib
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


IMPORT = load("reviewed_upload_import_under_test", "scripts/import-reviewed-knowledge.py")
UPLOADS = load("reviewed_uploads_under_test", "platform/core/knowledge_uploads.py")
RULES = "# Hours\nOpen 9 to 5.\n\n## Escalation\nAsk a human.\n"
NOW = 1793180000


class ReviewedUploadImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.uploads = self.data / "knowledge-uploads"
        self.workspace = self.root / "acme"
        shutil.copytree(ROOT / "examples/workspace", self.workspace)
        self.events = []
        self.audit = lambda action, actor, details: self.events.append((action, actor, details))
        self.kwargs = dict(tenant="acme", upload_id="", expected_sha256="", owner="owner@acme.example",
                           actor="operator:alice", name="reviewed-rules", agent="receptionist",
                           classification="internal", retention_days=7, now=NOW,
                           audit=self.audit)
        staged = UPLOADS.stage(self.uploads, tenant="acme", by="alice@acme.example",
                               filename="rules.md", content=RULES, request_id="a" * 32,
                               now=NOW - 100, retention_days=7, audit=self.audit)
        self.kwargs["upload_id"] = staged["uploadId"]
        self.kwargs["expected_sha256"] = staged["sha256"]
        self.directory = self.uploads / "acme"
        self.review = self.directory / ("r-" + staged["uploadId"] + ".json")
        self.content = self.directory / ("u-" + staged["uploadId"] + ".md")
        self.metadata = self.directory / ("u-" + staged["uploadId"] + ".json")
        UPLOADS.attest_owner_review(self.uploads, tenant="acme", upload_id=staged["uploadId"],
                                    expected_sha256=staged["sha256"], decision="approve",
                                    owner="owner@acme.example", now=NOW - 50,
                                    retention_days=7, audit=self.audit)

    def run_import(self, **overrides):
        return IMPORT.import_reviewed(self.data, self.workspace, **{**self.kwargs, **overrides})

    def test_dry_run_then_exact_owner_review_imports_only_draft_with_audit(self):
        before = (self.workspace / "agents/front-office/receptionist/agent.yaml").read_bytes()
        result = self.run_import(dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertFalse((self.workspace / "knowledge/reviewed-rules.yaml").exists())
        self.assertEqual(before, (self.workspace / "agents/front-office/receptionist/agent.yaml").read_bytes())
        self.assertFalse(any("import" in action for action, _, _ in self.events))
        result = self.run_import(dry_run=False)
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["sha256"], hashlib.sha256(RULES.encode()).hexdigest())
        self.assertEqual(yaml.safe_load((self.workspace / "knowledge/reviewed-rules.yaml").read_text())["spec"]["status"], "draft")
        self.assertEqual([a for a, _, _ in self.events if a.startswith("knowledge.reviewed-import")],
                         ["knowledge.reviewed-import-intent", "knowledge.reviewed-imported"])
        self.assertNotIn(RULES, json.dumps(self.events))
        self.assertTrue(self.run_import(dry_run=False)["alreadyImported"])

    def test_rejected_missing_or_interrupted_review_never_imports(self):
        self.review.unlink()
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False)
        self.review.write_text(json.dumps({"decision": "reject"}))
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False)
        self.review.unlink()
        (self.directory / (".r-" + self.kwargs["upload_id"] + ".pending")).write_text("{}")
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False)
        self.assertFalse((self.workspace / "knowledge/reviewed-rules.yaml").exists())

    def test_wrong_owner_tenant_digest_or_expired_draft_refused(self):
        for overrides in ({"owner": "someone@acme.example"}, {"tenant": "other"},
                          {"expected_sha256": "0" * 64}, {"now": NOW + 9 * 86400},
                          {"actor": "public:alice"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.run_import(dry_run=False, **overrides)
        self.assertFalse((self.workspace / "knowledge/reviewed-rules.yaml").exists())

    def test_changed_content_receipts_and_unsafe_paths_refused(self):
        self.content.write_text("# Hours\nChanged.\n")
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False)
        self.content.write_text(RULES)
        original = self.review.read_text()
        self.review.write_text(json.dumps({**json.loads(original), "ownerHash": "0" * 64}))
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False)
        self.review.write_text(original)
        other = self.root / "foreign"
        other.write_text(RULES)
        self.content.unlink()
        self.content.symlink_to(other)
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False)
        self.content.unlink()
        self.content.hardlink_to(other)
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False)
        self.assertFalse((self.workspace / "knowledge/reviewed-rules.yaml").exists())

    def test_wrong_workspace_and_audit_failure_refuse_success(self):
        foreign = self.root / "other"
        shutil.copytree(ROOT / "examples/workspace", foreign)
        with self.assertRaises(ValueError):
            IMPORT.import_reviewed(self.data, foreign, **self.kwargs, dry_run=False)
        with self.assertRaises(ValueError):
            self.run_import(dry_run=False, audit=None)
        def failed(*args):
            raise OSError("audit unavailable")
        with self.assertRaises(OSError):
            self.run_import(dry_run=False, audit=failed)
        self.assertFalse((self.workspace / "knowledge/reviewed-rules.yaml").exists())


if __name__ == "__main__":
    unittest.main()
