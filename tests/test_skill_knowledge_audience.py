"""Opt-in Skill audience restrictions require runtime-attested Skill context (#111/#116).

The deployed Hermes sidecar has no trustworthy active-Skill attestation today.
An Agent/model supplied claim must never authorize a source.
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kb_skill_audience_fixtures", ROOT / "tests/test_kb_runtime.py")
fixtures = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(fixtures)


class SkillKnowledgeAudienceTests(unittest.TestCase):
    def runtime(self, root: pathlib.Path, **source):
        return fixtures.RuntimeKbSearchTests().runtime(root, **source)

    def test_declared_skill_audience_blocks_unattested_hermes_and_read_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root, skillAudiences=["answer-faq"])
            result = runtime.kb_search(query="opening hours")["result"]
            self.assertEqual((result["documents"], result["reason"]), ([], "source-skill-context-required"))
            self.assertNotIn('"event": "read"', (root / "audit.jsonl").read_text())

    def test_only_trusted_enabled_skill_with_kb_tool_can_access_opt_in_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(pathlib.Path(tmp), skillAudiences=["answer-faq"])
            runtime.skill_resolver = lambda: "answer-faq"
            self.assertTrue(runtime.kb_search(query="opening hours")["result"]["documents"])
            # A different enabled Skill that has no kb.search authority cannot borrow the Agent's Tool.
            runtime.skill_resolver = lambda: "take-message"
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")
            # A resolver that names a Skill absent from the compiled Agent is also refused.
            runtime.skill_resolver = lambda: "unlisted-skill"
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")

    def test_wrong_attested_skill_cannot_read_other_skills_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root, skillAudiences=["answer-faq"])
            runtime.desired_state["skills"].append({
                "metadata": {"name": "other-faq"}, "spec": {"allowedTools": ["kb.search"], "requiredCapabilities": ["kb"]}
            })
            runtime.skill_resolver = lambda: "other-faq"
            result = runtime.kb_search(query="opening hours")["result"]
            self.assertEqual((result["documents"], result["reason"]), ([], "source-not-for-this-skill"))
            self.assertNotIn('"event": "read"', (root / "audit.jsonl").read_text())

    def test_model_cannot_claim_skill_in_tool_arguments_or_resolver_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(pathlib.Path(tmp), skillAudiences=["answer-faq"])
            with self.assertRaises(TypeError):
                runtime.kb_search(query="opening hours", skill="answer-faq")
            def failed_resolver():
                raise RuntimeError("provider secret should not be exposed")
            runtime.skill_resolver = failed_resolver
            with self.assertRaisesRegex(PermissionError, "trusted Skill context"):
                runtime.kb_search(query="opening hours")

    def test_source_state_and_readiness_block_until_trusted_skill_is_available(self):
        knowledge = fixtures.runtime_module.KNOWLEDGE
        spec = {"type": "text-file", "status": "approved", "skillAudiences": ["answer-faq"]}
        self.assertEqual(knowledge.source_state(spec, agent="receptionist"), "skill-context-required")
        self.assertEqual(knowledge.source_state(spec, agent="receptionist", skill="answer-faq"), "usable")
        self.assertEqual(knowledge.source_state(spec, agent="receptionist", skill="other-faq"), "not-for-this-skill")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = fixtures.workspace(tmp, skillAudiences=["answer-faq"])
            desired = fixtures.compiler.compile_workspace(workspace, "receptionist")[0]
            manifest = fixtures.runtime_module.CAPABILITIES.build_manifest(desired, fixtures.KB_BINDING)
            faq = next(s for s in manifest["skills"] if s["name"] == "answer-faq")
            self.assertFalse(faq["ready"])
            self.assertEqual(faq["dependencies"][0]["status"], "no-content")


if __name__ == "__main__":
    unittest.main()
