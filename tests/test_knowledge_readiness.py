"""Credential-free checks: a `kb` Skill is blocked when there is no approved content behind the provider."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


compiler = load("compiler_knowledge_readiness", "scripts/compile-workspace.py")
capabilities = load("capabilities_knowledge_readiness", "platform/core/capabilities.py")
knowledge = load("knowledge_readiness", "platform/core/knowledge.py")

BINDING = {"environment": "dev", "connectors": {
    "kb": {"provider": "text-knowledge", "tier": 0},
    "crm": {"provider": "built-in-crm", "tier": 0},
    "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://google/dev"},
}}
TODAY = dt.date(2026, 6, 1)


class SourceStateTests(unittest.TestCase):
    def state(self, **spec) -> str:
        return knowledge.source_state({"type": "text-file", **spec}, agent="receptionist", today=TODAY)

    def test_only_approved_effective_audience_matching_text_is_usable(self):
        self.assertEqual(self.state(status="approved"), "usable")
        self.assertEqual(self.state(), "draft")
        self.assertEqual(self.state(status="draft"), "draft")
        self.assertEqual(self.state(status="retired"), "retired")
        self.assertEqual(self.state(status="approved", effectiveDate="2026-06-02"), "not-yet-effective")
        self.assertEqual(self.state(status="approved", effectiveDate="2026-06-01"), "usable")
        self.assertEqual(self.state(status="approved", effectiveDate="soon"), "effective-date-invalid")
        self.assertEqual(self.state(status="approved", audiences=["sales"]), "not-for-this-agent")
        self.assertEqual(self.state(status="approved", audiences=["receptionist"]), "usable")

    def test_a_source_type_with_no_governed_content_is_never_usable(self):
        self.assertEqual(knowledge.source_state({"type": "repository", "status": "approved"}, today=TODAY), "not-indexed")

    def test_state_agrees_with_what_retrieval_will_actually_do(self):
        for spec in ({"status": "approved"}, {"status": "draft"}, {"status": "approved", "audiences": ["sales"]},
                     {"status": "approved", "effectiveDate": "2027-01-01"}):
            index = knowledge.build_index("s", "# A\nopening hours\n", filename="a.md", spec=spec)
            reason = knowledge.availability(index, agent="receptionist", today=TODAY)
            expected = "usable" if reason is None else reason.removeprefix("source-")
            self.assertEqual(self.state(**spec), expected, spec)


class ReadinessContentTests(unittest.TestCase):
    def workspace(self, root: pathlib.Path, **changes) -> pathlib.Path:
        workspace = root / "client"
        shutil.copytree(ROOT / "examples/workspace", workspace)
        path = workspace / "knowledge/rules-of-engagement.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is None:
                document["spec"].pop(key, None)
            else:
                document["spec"][key] = value
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return workspace

    def faq(self, workspace: pathlib.Path) -> dict:
        report = compiler.workspace_readiness(workspace, "receptionist", (), BINDING)
        return next(s for s in report[0]["skills"] if s["name"] == "answer-faq")

    def test_reference_workspace_has_usable_content_behind_the_kb_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            faq = self.faq(self.workspace(pathlib.Path(tmp)))
        self.assertTrue(faq["ready"] and faq["canEnable"])
        content = faq["dependencies"][0]["content"]
        self.assertEqual(content["usable"], 1)
        self.assertEqual({s["name"]: s["state"] for s in content["sources"]},
                         {"business-information": "not-indexed", "rules-of-engagement": "usable"})

    def test_unusable_content_blocks_the_dependent_skill_but_not_unrelated_skills(self):
        cases = {
            "draft": {"status": "draft"},
            "retired": {"status": "retired"},
            "absent-status": {"status": None},
            "future": {"effectiveDate": "2999-01-01"},
            "other-audience": {"audiences": ["sales"]},
        }
        for label, changes in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                workspace = self.workspace(pathlib.Path(tmp), **changes)
                report = compiler.workspace_readiness(workspace, "receptionist", (), BINDING)
                skills = {s["name"]: s for s in report[0]["skills"]}
                self.assertFalse(skills["answer-faq"]["ready"])
                self.assertFalse(skills["answer-faq"]["canEnable"])
                self.assertEqual(skills["answer-faq"]["dependencies"][0]["status"], "no-content")
                self.assertTrue(skills["take-message"]["ready"])
                self.assertFalse(skills["book-appointment"]["ready"])  # upstream not yet connected
                text = compiler.format_readiness(report)
                self.assertIn("NO APPROVED CONTENT", text)
                self.assertIn("BLOCKED", text)

    def test_compilation_still_succeeds_so_a_draft_can_be_authored_and_reviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), status="draft")
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
        skill = next(s for s in desired["capabilities"]["skills"] if s["name"] == "answer-faq")
        self.assertFalse(skill["ready"])

    def test_the_coworkers_own_manifest_reports_the_missing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), status="draft")
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            manifest = capabilities.build_manifest(desired, BINDING)
        faq = next(s for s in manifest["skills"] if s["name"] == "answer-faq")
        self.assertFalse(faq["ready"])
        self.assertEqual(faq["dependencies"][0]["status"], "no-content")
        self.assertNotIn("secret://", json.dumps(manifest))
        # A draft source's text never leaves the manifest: only names and states.
        self.assertEqual(set(faq["dependencies"][0]["content"]["sources"][0]), {"name", "state"})

    def test_a_connection_health_overlay_cannot_turn_a_no_content_skill_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), status="draft")
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            manifest = capabilities.build_manifest(desired, BINDING)
            overlaid = capabilities.apply_connection_health(
                manifest, {"calendar": {"health": "healthy", "updatedAt": "2026-06-01T00:00:00Z"}})
        faq = next(s for s in overlaid["skills"] if s["name"] == "answer-faq")
        self.assertFalse(faq["ready"])
        self.assertFalse(faq["canEnable"])

    def test_an_agent_that_selects_no_knowledge_at_all_is_blocked_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            path = workspace / "agents/front-office/receptionist/agent.yaml"
            agent = yaml.safe_load(path.read_text(encoding="utf-8"))
            agent["spec"]["knowledge"] = []
            path.write_text(yaml.safe_dump(agent, sort_keys=False), encoding="utf-8")
            report = compiler.workspace_readiness(workspace, "receptionist", (), BINDING)
        self.assertIn("no source selected", compiler.format_readiness(report))
        faq = next(s for s in report[0]["skills"] if s["name"] == "answer-faq")
        self.assertEqual(faq["dependencies"][0]["status"], "no-content")

    def test_an_optional_kb_dependency_reports_content_but_never_blocks(self):
        skill = {"metadata": {"name": "nice-to-have"}, "spec": {"optionalCapabilities": ["kb"]}}
        states = [{"name": "rules", "state": "draft"}]
        report = capabilities.skill_readiness({"metadata": {"name": "desk"}, "spec": {}}, [skill], BINDING, knowledge=states)
        self.assertTrue(report[0]["ready"])
        self.assertEqual(report[0]["dependencies"][0]["content"]["usable"], 0)

    def test_a_missing_provider_is_still_reported_as_missing_not_as_missing_content(self):
        skill = {"metadata": {"name": "answer-faq"}, "spec": {"requiredCapabilities": ["kb"]}}
        report = capabilities.skill_readiness({"metadata": {"name": "desk"}, "spec": {}}, [skill],
                                              {"environment": "dev", "connectors": {}}, knowledge=[])
        self.assertEqual(report[0]["dependencies"][0]["status"], "missing")
        self.assertNotIn("content", report[0]["dependencies"][0])

    def test_readiness_without_knowledge_information_keeps_the_previous_shape(self):
        skill = {"metadata": {"name": "answer-faq"}, "spec": {"requiredCapabilities": ["kb"]}}
        report = capabilities.skill_readiness({"metadata": {"name": "desk"}, "spec": {}}, [skill], BINDING)
        self.assertTrue(report[0]["ready"])
        self.assertNotIn("content", report[0]["dependencies"][0])


class MaterializedContentGateTests(unittest.TestCase):
    """A provider declaration is not proof that approved business rules are actually readable."""

    workspace = ReadinessContentTests.workspace

    def faq(self, workspace: pathlib.Path) -> dict:
        report = compiler.workspace_readiness(workspace, "receptionist", (), BINDING)
        return next(skill for skill in report[0]["skills"] if skill["name"] == "answer-faq")

    def test_empty_approved_file_blocks_skill_in_operator_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            (workspace / "knowledge/rules-of-engagement.md").write_text("  \n", encoding="utf-8")
            faq = self.faq(workspace)
        self.assertFalse(faq["ready"])
        self.assertEqual(faq["dependencies"][0]["status"], "no-content")
        self.assertEqual(faq["dependencies"][0]["content"]["sources"][-1]["state"], "invalid-content")
        self.assertIn("Repair or upload", faq["remediation"])

    def test_missing_approved_file_is_blocked_without_leaking_a_host_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            (workspace / "knowledge/rules-of-engagement.md").unlink()
            faq = self.faq(workspace)
        self.assertFalse(faq["canEnable"])
        self.assertEqual(faq["dependencies"][0]["content"]["sources"][-1]["state"], "invalid-content")
        self.assertNotIn(str(workspace), json.dumps(faq))

    def test_manifest_rechecks_missing_materialized_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            desired.pop("knowledgeContent")
            manifest = capabilities.build_manifest(desired, BINDING)
        faq = next(skill for skill in manifest["skills"] if skill["name"] == "answer-faq")
        self.assertFalse(faq["ready"])
        self.assertEqual(faq["dependencies"][0]["status"], "no-content")

    def test_manifest_rejects_mismatched_content_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            desired["knowledgeContent"]["rules-of-engagement"]["sha256"] = "0" * 64
            manifest = capabilities.build_manifest(desired, BINDING)
        faq = next(skill for skill in manifest["skills"] if skill["name"] == "answer-faq")
        self.assertFalse(faq["ready"])
        self.assertEqual(faq["dependencies"][0]["content"]["sources"][-1]["state"], "invalid-content")

    def test_valid_materialized_knowledge_remains_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            manifest = capabilities.build_manifest(desired, BINDING)
        faq = next(skill for skill in manifest["skills"] if skill["name"] == "answer-faq")
        self.assertTrue(faq["ready"])


class RemediationTests(unittest.TestCase):
    """Every blocked Skill names the one non-secret action that unblocks it."""

    workspace = ReadinessContentTests.workspace

    def test_each_kind_of_block_gets_its_own_single_action(self):
        skill = {"metadata": {"name": "answer-faq"}, "spec": {"requiredCapabilities": ["kb", "calendar"]}}
        agent = {"metadata": {"name": "desk"}, "spec": {}}
        unbound = capabilities.skill_readiness(agent, [skill], {"environment": "dev", "connectors": {}}, knowledge=[])
        self.assertIn("connect calendar", unbound[0]["remediation"])  # sorted by capability: calendar before kb
        kb_only = {"metadata": {"name": "answer-faq"}, "spec": {"requiredCapabilities": ["kb"]}}
        drafted = capabilities.skill_readiness(agent, [kb_only], BINDING, knowledge=[{"name": "rules", "state": "draft"}])
        self.assertIn("Approve business knowledge", drafted[0]["remediation"])
        ready = capabilities.skill_readiness(agent, [kb_only], BINDING, knowledge=[{"name": "rules", "state": "usable"}])
        self.assertNotIn("remediation", ready[0])

    def test_remediation_reaches_the_report_the_compiled_output_and_the_coworkers_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), status="draft")
            report = compiler.workspace_readiness(workspace, "receptionist", (), BINDING)
            text = compiler.format_readiness(report)
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            manifest = capabilities.build_manifest(desired, BINDING)
        faq = next(s for s in report[0]["skills"] if s["name"] == "answer-faq")
        self.assertIn("Approve business knowledge", faq["remediation"])
        self.assertIn("Next: Approve business knowledge", text)
        self.assertIn("Approve business knowledge", next(s for s in desired["capabilities"]["skills"] if s["name"] == "answer-faq")["remediation"])
        self.assertIn("Approve business knowledge", next(s for s in manifest["skills"] if s["name"] == "answer-faq")["remediation"])
        self.assertNotIn("secret://", json.dumps(manifest))
        ready = next(s for s in report[0]["skills"] if s["name"] == "take-message")
        self.assertNotIn("remediation", ready)

    def test_a_revoked_connection_changes_the_action_and_a_recovery_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp))
            desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=BINDING)[0]
            manifest = capabilities.build_manifest(desired, BINDING)
            revoked = capabilities.apply_connection_health(manifest, {"calendar": {"health": "revoked", "updatedAt": "2026-06-01T00:00:00Z"}})
            booking = next(s for s in revoked["skills"] if s["name"] == "book-appointment")
            self.assertFalse(booking["ready"])
            self.assertIn("Connect calendar", booking["remediation"])
            healed = capabilities.apply_connection_health(manifest, {"calendar": {"health": "healthy", "updatedAt": "2026-06-01T00:00:00Z"}})
            recovered = next(s for s in healed["skills"] if s["name"] == "book-appointment")
            # A healthy flag cannot authorize an unimplemented upstream adapter.
            self.assertFalse(recovered["ready"])
            self.assertIn("Connect calendar", recovered["remediation"])


if __name__ == "__main__":
    unittest.main()
