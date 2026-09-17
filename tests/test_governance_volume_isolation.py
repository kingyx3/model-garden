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
    def _compose(self, *, calendar: bool = True) -> dict:
        return yaml.safe_load(
            deployer._compose(
                "acme-prod",
                "openai",
                {"version": "0.21.2", "commit": "a" * 40},
                calendar=("secret://google/prod", "primary") if calendar else None,
            )
        )

    def test_approval_and_audit_volume_is_not_mounted_into_hermes(self) -> None:
        compose = self._compose()
        hermes_volumes = compose["services"]["hermes"]["volumes"]
        governance_volumes = compose["services"]["governed-tools"]["volumes"]

        self.assertEqual(hermes_volumes, ["hermes-data:/opt/data"])
        self.assertEqual(governance_volumes, ["governance-data:/opt/data"])
        self.assertIn("hermes-data", compose["volumes"])
        self.assertIn("governance-data", compose["volumes"])
        self.assertNotIn("governance-data:/opt/data", hermes_volumes)
        self.assertNotIn("hermes-data:/opt/data", governance_volumes)

    def test_governed_tools_are_not_on_channel_runtime_network(self) -> None:
        compose = self._compose()
        hermes = compose["services"]["hermes"]
        tools = compose["services"]["governed-tools"]

        self.assertEqual(hermes["networks"], ["default", "agent-tools"])
        self.assertEqual(tools["networks"], ["agent-tools"])
        self.assertEqual(set(compose["networks"]), {"agent-tools"})
        self.assertNotIn("default", tools["networks"])

    def test_runtime_services_are_non_root_and_capability_dropped(self) -> None:
        compose = self._compose()
        for service_name in ("hermes", "governed-tools"):
            service = compose["services"][service_name]
            self.assertEqual(service["user"], "10001:10001")
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
            self.assertTrue(service["init"])
            self.assertEqual(service["pids_limit"], 256)
            self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=64m", service["tmpfs"])

        volume_init = compose["services"]["volume-init"]
        self.assertEqual(volume_init["network_mode"], "none")
        self.assertEqual(volume_init["cap_drop"], ["ALL"])
        self.assertEqual(volume_init["cap_add"], ["CHOWN"])
        self.assertEqual(volume_init["restart"], "no")
        self.assertIn("service_completed_successfully", str(compose["services"]["hermes"]["depends_on"]))

    def test_runtime_without_governed_tools_does_not_create_governance_volume(self) -> None:
        compose = self._compose(calendar=False)

        self.assertEqual(set(compose["volumes"]), {"hermes-data"})
        self.assertNotIn("governed-tools", compose["services"])
        self.assertNotIn("agent-tools", compose.get("networks", {}))
        self.assertEqual(compose["services"]["volume-init"]["volumes"], ["hermes-data:/hermes"])


if __name__ == "__main__":
    unittest.main()
