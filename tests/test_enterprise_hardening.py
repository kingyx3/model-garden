from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION_REF = re.compile(r"^[^\s@]+@[0-9a-f]{40}$")


class EnterpriseHardeningTests(unittest.TestCase):
    def test_all_github_actions_are_commit_pinned(self) -> None:
        paths = list((ROOT / ".github" / "workflows").glob("*.yml"))
        paths += list((ROOT / ".github" / "workflows").glob("*.yaml"))
        paths += list((ROOT / "templates" / "client-workflows").glob("*.yml"))
        paths += list((ROOT / "templates" / "client-workflows").glob("*.yaml"))
        self.assertTrue(paths)
        for path in paths:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if not stripped.startswith("uses:") and " uses:" not in f" {stripped}":
                    continue
                value = stripped.split("uses:", 1)[1].split("#", 1)[0].strip()
                if value.startswith("./"):
                    continue
                self.assertRegex(
                    value,
                    ACTION_REF,
                    msg=f"{path.relative_to(ROOT)}:{line_number} must pin external actions to a full commit SHA",
                )

    def test_required_contract_status_aggregates_assurance(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("contracts-and-runtime:\n    needs: [contracts-runtime-tests, enterprise-evidence]", workflow)
        self.assertIn("run: bash scripts/run-assurance-scans.sh --enforce", workflow)
        self.assertIn("actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d", workflow)

    def test_gcp_deployer_avoids_broad_compute_admin(self) -> None:
        terraform = (ROOT / "infra" / "bootstrap" / "gcp" / "main.tf").read_text(encoding="utf-8")
        self.assertNotIn('"roles/compute.admin"', terraform)
        self.assertIn('"roles/compute.instanceAdmin.v1"', terraform)
        self.assertIn('"roles/compute.networkAdmin"', terraform)
        self.assertIn('"roles/compute.securityAdmin"', terraform)
        self.assertIn('role               = "roles/iam.serviceAccountUser"', terraform)

    def test_voice_worker_keeps_channel_off_agent_tool_network(self) -> None:
        source = (ROOT / "scripts" / "deploy-reference-voice.py").read_text(encoding="utf-8")
        self.assertIn('"networks": ["default"]', source)
        self.assertIn("**DEPLOY._container_hardening()", source)
        self.assertIn('"network_mode": "none"', source)
        self.assertNotIn('"networks": ["agent-tools"]', source)

    def test_direct_python_dependencies_are_exact_pins(self) -> None:
        for filename in ("requirements-runtime.txt", "requirements-voice.txt", "requirements-assurance.txt"):
            for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                self.assertIn("==", line, msg=f"{filename}: direct dependency must be exact-pinned: {line}")

        for line in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            self.assertIn("==", line, msg=f"requirements-dev.txt: direct dependency must be exact-pinned: {line}")


if __name__ == "__main__":
    unittest.main()
