from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "bootstrap" / "gcp" / "main.tf"


class GcpOidcTrustTests(unittest.TestCase):
    def test_deployer_trust_is_scoped_to_model_garden_workflow_run(self) -> None:
        source = TERRAFORM.read_text(encoding="utf-8")

        self.assertIn(
            'deployment_workflow_ref = "${var.github_repository}/.github/workflows/model-garden-cloud.yml@refs/heads/main"',
            source,
        )
        self.assertIn("assertion.repository == '${var.github_repository}'", source)
        self.assertIn("assertion.workflow_ref == '${local.deployment_workflow_ref}'", source)
        self.assertIn("assertion.event_name == 'workflow_run'", source)
        self.assertNotIn(
            'attribute_condition = "assertion.repository == \'${var.github_repository}\'"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
