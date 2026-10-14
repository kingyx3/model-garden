from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


class EnterpriseSDLCTests(unittest.TestCase):
    def test_third_party_actions_are_commit_pinned(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        mutable = re.findall(r"uses:\s+[^\s]+@v\d+(?:\s|$)", workflow)
        self.assertEqual(mutable, [], f"mutable GitHub Action tags found: {mutable}")
        for expected in (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e",
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        ):
            self.assertIn(expected, workflow)

    def test_generated_production_workflow_pins_actions_and_checks_source(self):
        workflow = (ROOT / "templates" / "client-workflows" / "gcp-keyless.yml").read_text(encoding="utf-8")
        actions = re.findall(r"(?m)^\s*uses:\s+[^@\s]+@([^\s#]+)", workflow)
        self.assertTrue(actions)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", action) for action in actions))
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", workflow)
        self.assertIn("persist-credentials: false", workflow)

    def test_assurance_scans_run_inside_ruleset_required_job(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["contracts-and-runtime"]
        rendered = repr(job)
        self.assertIn("run-assurance-scans.sh", rendered)
        self.assertIn("enterprise-evidence-pack.md", rendered)
        self.assertIn("retention-days", rendered)
        self.assertIn("90", rendered)
        self.assertIn("always()", rendered)

    def test_voice_dependencies_are_exactly_pinned(self):
        requirements = {
            line.strip()
            for line in (ROOT / "requirements-voice.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        livekit = next((item for item in requirements if item.startswith("livekit-agents[openai]==")), None)
        httpx = next((item for item in requirements if item.startswith("httpx==")), None)
        self.assertIsNotNone(livekit)
        self.assertIsNotNone(httpx)
        self.assertRegex(livekit or "", r"^livekit-agents\[openai\]==\d+\.\d+\.\d+$")
        self.assertRegex(httpx or "", r"^httpx==\d+\.\d+\.\d+$")
        self.assertEqual(len(requirements), 2, "unexpected unpinned voice dependency added")

    def test_voice_fallback_storage_has_privacy_and_retention_guardrails(self):
        worker = (ROOT / "platform" / "channels" / "livekit_receptionist.py").read_text(encoding="utf-8")
        self.assertIn("MODEL_GARDEN_MESSAGE_RETENTION_SECONDS", worker)
        self.assertIn("roomHash", worker)
        self.assertIn("os.fchmod(descriptor, 0o600)", worker)
        self.assertIn("fcntl.LOCK_EX", worker)
        self.assertNotIn('"room": job_ctx.room.name', worker)
        self.assertNotIn('"target": target', worker)

    def test_security_governance_files_exist(self):
        self.assertTrue((ROOT / "SECURITY.md").is_file())
        dependabot = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
        ecosystems = {item["package-ecosystem"] for item in dependabot["updates"]}
        self.assertTrue({"github-actions", "pip", "terraform"}.issubset(ecosystems))


if __name__ == "__main__":
    unittest.main()
