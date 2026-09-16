from __future__ import annotations

import importlib.util
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-client-docker.py"
spec = importlib.util.spec_from_file_location("deploy_governance_isolation", SCRIPT)
deployer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(deployer)


class GovernanceVolumeIsolationTests(unittest.TestCase):
    def test_approval_and_audit_volume_is_not_mounted_into_hermes(self) -> None:
        compose = yaml.safe_load(
            deployer._compose(
                "acme-prod",
                "openai",
                {"version": "0.21.2", "commit": "a" * 40},
                calendar=("secret://google/prod", "primary"),
            )
        )

        hermes_volumes = compose["services"]["hermes"]["volumes"]
        governance_volumes = compose["services"]["governed-tools"]["volumes"]

        self.assertEqual(hermes_volumes, ["hermes-data:/opt/data"])
        self.assertEqual(governance_volumes, ["governance-data:/opt/data"])
        self.assertIn("hermes-data", compose["volumes"])
        self.assertIn("governance-data", compose["volumes"])
        self.assertNotIn("governance-data:/opt/data", hermes_volumes)
        self.assertNotIn("hermes-data:/opt/data", governance_volumes)

    def test_runtime_without_governed_tools_does_not_create_governance_volume(self) -> None:
        compose = yaml.safe_load(
            deployer._compose(
                "acme-dev",
                "openai",
                {"version": "0.21.2", "commit": "a" * 40},
                calendar=None,
            )
        )

        self.assertEqual(set(compose["volumes"]), {"hermes-data"})
        self.assertNotIn("governed-tools", compose["services"])


if __name__ == "__main__":
    unittest.main()
