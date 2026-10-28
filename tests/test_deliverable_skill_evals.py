"""Synthetic report/spreadsheet acceptance evals through the real governed docs.create boundary (#101).

These exercise compilation, owner approval, Tier 0 storage, sources, ledger and file
structure. They do not exercise a language model, Google Drive or SharePoint.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
import zipfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "deliverable_skill_docs_runtime_cases", ROOT / "tests" / "test_docs_runtime.py"
)
base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = base
_spec.loader.exec_module(base)
BINDING = {
    "environment": "dev",
    "connectors": {
        "crm": {"provider": "built-in-crm", "tier": 0},
        "docs": {"provider": "built-in-artifacts", "tier": 0, "owner": "owner@client.example"},
    },
}


class DeliverableSkillEvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", self.workspace)
        self.agent_path = self.workspace / "agents" / "sales" / "account-researcher" / "agent.yaml"

    def enable(self):
        agent = yaml.safe_load(self.agent_path.read_text(encoding="utf-8"))
        agent["spec"]["disabledSkills"] = [
            name for name in agent["spec"].get("disabledSkills", []) if name != "create-deliverable"
        ]
        self.agent_path.write_text(yaml.safe_dump(agent, sort_keys=False), encoding="utf-8")

    def compiled(self, binding=BINDING):
        return base.compiler.compile_workspace(
            self.workspace, "account-researcher", environment_binding=binding
        )[0]

    def runtime(self, *, principal=None):
        desired = self.compiled()
        desired["source"]["client"] = "acme"
        desired_path = self.root / "desired.json"
        desired_path.write_text(json.dumps(desired), encoding="utf-8")
        manifest = base.compiler.CAPABILITIES.build_manifest(desired, BINDING)
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return base.runtime_module.GovernedToolRuntime(
            desired_path, self.root / "audit.jsonl", self.root / "approvals",
            environ={
                "MODEL_GARDEN_INITIATING_IDENTITY": "alice@acme.example",
                "MODEL_GARDEN_CAPABILITY_MANIFEST": str(manifest_path),
            },
            principal_resolver=(lambda: principal) if principal is not None else None,
        )

    def audit(self):
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_reference_skill_is_opt_in_and_exclusive_tool_not_exposed(self):
        desired = base.compiler.compile_workspace(self.workspace, "account-researcher")[0]
        self.assertNotIn("create-deliverable", [x["metadata"]["name"] for x in desired["skills"]])
        self.assertNotIn("docs.create", [x["metadata"]["name"] for x in desired["tools"]])
        declarations = {x["metadata"]["name"]: x for x in desired["disabledSkillDeclarations"]}
        self.assertEqual(declarations["create-deliverable"]["spec"]["requiredCapabilities"], ["docs"])
        self.assertNotIn("create-deliverable", desired["skillInstructions"])

    def test_enabling_requires_an_actual_docs_binding_and_materializes_instructions(self):
        self.enable()
        with self.assertRaisesRegex(ValueError, "create-deliverable"):
            self.compiled({"environment": "dev", "connectors": {
                "crm": {"provider": "built-in-crm", "tier": 0}
            }})
        desired = self.compiled()
        self.assertIn("create-deliverable", desired["skillInstructions"])
        self.assertIn("docs.create", [x["metadata"]["name"] for x in desired["tools"]])
        self.assertIn("source", desired["skillInstructions"]["create-deliverable"]["content"].lower())

    def test_report_and_spreadsheet_governed_evals(self):
        self.enable()
        runtime = self.runtime()
        suite = json.loads((ROOT / "evals" / "deliverables" / "scenarios.json").read_text(encoding="utf-8"))
        self.assertEqual(suite["schemaVersion"], 1)
        self.assertEqual({case["id"] for case in suite["scenarios"]}, {"report", "spreadsheet"})
        store = base.runtime_module.ARTIFACTS.ArtifactStore(self.root / "artifacts", tenant="acme")
        for case in suite["scenarios"]:
            with self.subTest(case=case["id"]):
                args = {
                    "title": case["title"], "format": case["format"], "structure": case["structure"],
                    "sources": case["sources"], "justification": "synthetic deliverable evaluation",
                }
                pending = runtime.docs_create(**args)
                self.assertEqual(pending["status"], "pending-approval")
                self.assertEqual(store.list(), []) if case["id"] == "report" else None
                base.approval_module.decide(
                    self.root / "approvals", pending["request"]["requestId"],
                    approved=True, approver="owner@client.example",
                )
                completed = runtime.docs_create(**args)
                self.assertEqual(completed["status"], "executed")
                result = completed["result"]
                self.assertTrue(result["documentId"].startswith("tier0:acme/a-"))
                entry, content = store.get(result["artifactId"], requested_by="alice@acme.example")
                self.assertEqual(entry["location"], result["documentId"])
                self.assertEqual(entry["sources"], case["sources"])
                self.assertTrue(entry["name"].endswith("." + case["format"]))
                with zipfile.ZipFile(io.BytesIO(content)) as package:
                    self.assertIsNone(package.testzip())
                    member = package.read(case["packageMember"]).decode("utf-8")
                self.assertIn(case["expectedText"], member)
                if case["format"] == "xlsx":
                    self.assertNotIn("<f>", member)
                    self.assertIn("HYPERLINK", member)  # hostile text stays inert
                audit = self.audit()
                created = [e for e in audit if e.get("action") == "artifact.create"
                           and e.get("details", {}).get("location") == result["documentId"]]
                executed = [e for e in audit if e.get("event") == "execution"
                            and e.get("ledger", {}).get("externalObjectRef") == result["documentId"]]
                self.assertEqual(len(created), 1)
                self.assertEqual(len(executed), 1)
                self.assertEqual(created[0]["details"]["sources"], case["sources"])
                self.assertNotIn(case["expectedText"], json.dumps(audit))
        self.assertTrue(base.runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])
        self.assertEqual(len(store.list()), 2)

    def test_approval_does_not_authorize_modified_content_or_sources(self):
        self.enable()
        runtime = self.runtime()
        args = {"title": "Synthetic report", "format": "docx",
                "structure": {"sections": [{"heading": "Summary", "paragraphs": ["original"]}]},
                "sources": ["kb:synthetic#original"]}
        pending = runtime.docs_create(**args)
        base.approval_module.decide(
            self.root / "approvals", pending["request"]["requestId"],
            approved=True, approver="owner@client.example",
        )
        for changes in (
            {"structure": {"sections": [{"heading": "Summary", "paragraphs": ["altered"]}]}},
            {"sources": ["kb:synthetic#different"]},
            {"format": "pdf"},
        ):
            changed = {**args, **changes}
            self.assertEqual(runtime.docs_create(**changed)["status"], "pending-approval")
        store = base.runtime_module.ARTIFACTS.ArtifactStore(self.root / "artifacts", tenant="acme")
        self.assertEqual(store.list(), [])

    def test_public_principal_and_cross_tenant_reads_are_denied(self):
        self.enable()
        public = base.principal_for("U-STRANGER")
        with self.assertRaises(PermissionError):
            self.runtime(principal=public).docs_create(
                title="T", format="xlsx", structure={"sheets": [{"name": "S", "rows": [[1]]}]},
            )
        runtime = self.runtime()
        args = {"title": "Synthetic sheet", "format": "xlsx", "structure": {"sheets": [{"name": "S", "rows": [[1]]}]}}
        pending = runtime.docs_create(**args)
        base.approval_module.decide(
            self.root / "approvals", pending["request"]["requestId"],
            approved=True, approver="owner@client.example",
        )
        entry = runtime.docs_create(**args)["result"]
        foreign = base.runtime_module.ARTIFACTS.ArtifactStore(self.root / "artifacts", tenant="other")
        with self.assertRaises(ValueError):
            foreign.get(entry["artifactId"], requested_by="alice@acme.example")


if __name__ == "__main__":
    unittest.main()
