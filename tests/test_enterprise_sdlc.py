from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


class EnterpriseSDLCTests(unittest.TestCase):
    def test_third_party_actions_are_commit_pinned(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        parsed = yaml.safe_load(workflow)
        actions = [step["uses"] for job in parsed["jobs"].values() for step in job.get("steps", []) if "uses" in step]
        self.assertTrue(actions, "validation workflow must use pinned GitHub Actions")
        for action in actions:
            if action.startswith("./"):
                continue  # Local actions don't have remote commit references.
            self.assertRegex(action, r"^[^@\s]+@[0-9a-f]{40}$", f"action is not pinned to a commit: {action}")
        for required in ("actions/checkout", "hashicorp/setup-terraform", "actions/upload-artifact"):
            self.assertTrue(
                any(action.startswith(f"{required}@") for action in actions),
                f"expected action missing: {required}",
            )

    def test_generated_production_workflow_pins_actions_and_checks_source(self):
        workflow = (ROOT / "templates" / "client-workflows" / "gcp-keyless.yml").read_text(encoding="utf-8")
        actions = re.findall(r"(?m)^\s*uses:\s+[^@\s]+@([^\s#]+)", workflow)
        self.assertTrue(actions)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", action) for action in actions))
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", workflow)
        self.assertIn("persist-credentials: false", workflow)

    def test_required_enterprise_evidence_context_is_an_unconditional_fail_closed_job(self):
        """The ruleset requires this exact GitHub Actions job name, not a nested step."""
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        self.assertIn("enterprise-evidence", jobs)
        job = jobs["enterprise-evidence"]
        self.assertNotIn("if", job, "a required check must be reported even if other jobs fail")
        self.assertNotIn("needs", job, "assurance must still report if runtime validation fails")
        self.assertNotIn("continue-on-error", job, "scan findings must fail the required check")
        self.assertEqual(job.get("timeout-minutes"), 30)
        steps = job["steps"]
        scan = next(step for step in steps if "run-assurance-scans.sh" in step.get("run", ""))
        self.assertNotIn("if", scan)
        self.assertNotIn("continue-on-error", scan)
        self.assertIn("--require-clean-scans", (ROOT / "scripts" / "run-assurance-scans.sh").read_text())
        verify = next(step for step in steps if "enterprise-evidence-pack.md" in step.get("run", ""))
        self.assertIn(".enterprise-evidence/manifest.json", verify["run"])
        self.assertIn("python3 -m json.tool", verify["run"])
        upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], 90)
        self.assertEqual(upload.get("if"), "always()", "retain scanner evidence even on a failed scan")
        self.assertIn("enterprise-evidence", jobs["publish-main-release"]["needs"], "publishing must wait for a clean evidence gate")
        self.assertNotIn("run-assurance-scans.sh", repr(jobs["contracts-and-runtime"]))
        self.assertIn("enterprise-evidence", (ROOT / "scripts" / "repo-governance-check.py").read_text())

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
        # The voice bridge delegates to the same hardened writer as transcripts.
        # Keep checking the actual file/lock safeguards, not a stale implementation
        # detail that requires the bridge to duplicate its own persistence logic.
        writer = (ROOT / "platform" / "channels" / "call_transcript.py").read_text(encoding="utf-8")
        self.assertIn("store_private_jsonl(MESSAGE_PATH, payload, retention_seconds=retention)", worker)
        self.assertIn("return retention > 0", worker)
        self.assertIn("stat.S_IMODE(lock_stat.st_mode) != 0o600", writer)
        self.assertIn("stat.S_IMODE(target_stat.st_mode) != 0o600", writer)
        self.assertIn("fcntl.LOCK_EX", writer)
        self.assertIn("os.O_NOFOLLOW", writer)
        self.assertIn("_open_private_parent(path)", writer)
        self.assertIn("os.open(component, flags, dir_fd=directory)", writer)
        self.assertIn("os.O_EXCL", writer)
        self.assertIn("lock_stat.st_nlink != 1", writer)
        self.assertIn("target_stat.st_nlink != 1", writer)
        self.assertIn("assert_directory()", writer)
        self.assertIn("assert_target()", writer)
        self.assertIn("os.fsync(temp_fd)", writer)
        self.assertIn("os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)", writer)
        self.assertIn("os.fsync(directory)", writer)
        self.assertNotIn('"room": job_ctx.room.name', worker)
        self.assertNotIn('"target": target', worker)

    def test_security_governance_files_exist(self):
        self.assertTrue((ROOT / "SECURITY.md").is_file())
        dependabot = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
        ecosystems = {item["package-ecosystem"] for item in dependabot["updates"]}
        self.assertTrue({"github-actions", "pip", "terraform"}.issubset(ecosystems))


if __name__ == "__main__":
    unittest.main()
