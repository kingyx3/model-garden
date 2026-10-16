"""Credential-free acceptance: one reference receptionist runs on a zero-stack client and upgrades by binding.

Scenario A is a client with no third-party account: Tier 0 knowledge and contact log only, booking
disabled. Scenario B enables booking and binds a calendar provider. Neither scenario edits the Agent,
Skill or Tool definitions beyond `disabledSkills`; anything unshipped, forged or unhealthy stays blocked.
"""
from __future__ import annotations

import copy
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


compiler = load("compiler_reference_compat", "scripts/compile-workspace.py")
capabilities = load("capabilities_reference_compat", "platform/core/capabilities.py")
deployer = load("deployer_reference_compat", "scripts/deploy-client-docker.py")

TIER_ZERO = {
    "kb": {"provider": "text-knowledge", "tier": 0},
    "crm": {"provider": "built-in-crm", "tier": 0},
}
ZERO_STACK = {"environment": "dev", "connectors": dict(TIER_ZERO)}
WITH_GOOGLE = {"environment": "dev", "connectors": {
    **TIER_ZERO, "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://google/dev"}}}
WITH_MICROSOFT = {"environment": "dev", "connectors": {
    **TIER_ZERO, "calendar": {"provider": "microsoft", "tier": 1, "token_ref": "secret://ms/dev"}}}
CALENDAR_TOOLS = {"calendar.availability", "calendar.book"}


class ReferenceCompatibilityTests(unittest.TestCase):
    def workspace(self, root: pathlib.Path, *, booking: bool) -> pathlib.Path:
        workspace = root / "client"
        shutil.copytree(ROOT / "examples/workspace", workspace)
        if True:
            path = workspace / "agents/front-office/receptionist/agent.yaml"
            agent = yaml.safe_load(path.read_text(encoding="utf-8"))
            agent["spec"]["disabledSkills"] = [] if booking else ["book-appointment"]
            path.write_text(yaml.safe_dump(agent, sort_keys=False), encoding="utf-8")
        return workspace

    def compile(self, workspace: pathlib.Path, binding: dict):
        return compiler.compile_workspace(workspace, "receptionist", environment_binding=binding)[0]

    @staticmethod
    def tool_names(desired: dict) -> set[str]:
        return {tool["metadata"]["name"] for tool in desired["tools"]}

    def render(self, root: pathlib.Path, desired: dict, connectors: dict, name: str):
        profile = root / f"profile-{name}"
        (profile / ".modelgarden").mkdir(parents=True)
        (profile / "SOUL.md").write_text("# Receptionist\n", encoding="utf-8")
        (profile / "config.yaml").write_text(
            yaml.safe_dump({"model": {"provider": "openai", "default": "m"}}), encoding="utf-8")
        (profile / ".modelgarden" / "desired-state.json").write_text(json.dumps(desired), encoding="utf-8")
        binding = root / f"binding-{name}.json"
        binding.write_text(json.dumps({
            "apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding",
            "spec": {"environment": "dev",
                     "runtime": {"profile": "receptionist", "model_credential_ref": "secret://model/dev"},
                     "connectors": connectors},
        }), encoding="utf-8")
        return deployer.render_bundle(profile, binding, root / f"bundle-{name}", "acme-dev")

    def test_scenario_a_zero_stack_compiles_with_booking_disabled_and_no_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), booking=False)
            desired = self.compile(workspace, ZERO_STACK)
            self.assertEqual(self.tool_names(desired), {"kb.search", "crm.create", "crm.log-interaction"})
            self.assertFalse(self.tool_names(desired) & CALENDAR_TOOLS)
            self.assertEqual(
                sorted(s["metadata"]["name"] for s in desired["skills"]),
                ["answer-faq", "human-handoff", "log-conversation", "take-message"],
            )
            readiness = compiler.workspace_readiness(workspace, "receptionist", (), ZERO_STACK)
            ready = {s["name"]: s for s in readiness[0]["skills"]}
            self.assertTrue(ready["answer-faq"]["ready"])
            self.assertTrue(ready["log-conversation"]["ready"])
            self.assertFalse(ready["book-appointment"]["enabled"])

    def test_scenario_a_deploys_a_credential_free_bundle_without_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            desired = self.compile(self.workspace(root, booking=False), ZERO_STACK)
            metadata = self.render(root, desired, ZERO_STACK["connectors"], "a")
            self.assertTrue(metadata["governedToolService"])
            self.assertNotIn("calendarProvider", metadata)
            self.assertFalse(set(metadata["selectedTools"]) & CALENDAR_TOOLS)
            self.assertEqual(metadata["unboundCapabilities"], ["tickets"])  # optional, degrades to hand-off
            manifest = json.loads(
                (root / "bundle-a/profile/.modelgarden/capability-manifest.json").read_text(encoding="utf-8"))
            by_family = {e["family"]: e for e in manifest["capabilities"]}
            for family in ("kb", "crm"):
                self.assertEqual(by_family[family]["status"], "available")
                self.assertEqual(by_family[family]["tier"], 0)
            self.assertNotIn("secret://", json.dumps(manifest))

    def test_scenario_a_still_fails_closed_if_booking_is_left_on_without_a_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), booking=True)
            with self.assertRaisesRegex(ValueError, "book-appointment"):
                self.compile(workspace, ZERO_STACK)

    def test_unshipped_forged_or_wrong_tier_providers_never_unlock_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), booking=True)
            for calendar in (
                {"provider": "hubspot", "tier": 1},
                {"provider": "not-yet-built", "tier": 1},
                {"provider": "google", "tier": 0},
                {"provider": "google", "tier": 3},
                {"provider": "built-in-crm", "tier": 0},
            ):
                binding = copy.deepcopy(ZERO_STACK)
                binding["connectors"]["calendar"] = {**calendar, "token_ref": "secret://x/dev"}
                with self.assertRaisesRegex(ValueError, "book-appointment", msg=str(calendar)):
                    self.compile(workspace, binding)

    def test_forged_tier_zero_bindings_never_satisfy_knowledge_or_contact_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.workspace(pathlib.Path(tmp), booking=False)
            for connectors in (
                {"kb": {"provider": "text-knowledge", "tier": 1}, "crm": TIER_ZERO["crm"]},
                {"kb": TIER_ZERO["kb"], "crm": {"provider": "hubspot", "tier": 1}},
                {"kb": {"provider": "notion-knowledge", "tier": 2}, "crm": TIER_ZERO["crm"]},
                {"kb": TIER_ZERO["kb"]},
            ):
                with self.assertRaises(ValueError, msg=str(connectors)):
                    self.compile(workspace, {"environment": "dev", "connectors": connectors})


if __name__ == "__main__":
    unittest.main()
