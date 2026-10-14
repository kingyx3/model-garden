"""Credential-free checks: a configured but unshipped connector is never usable."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


capabilities = load("capabilities_connector_activation", "platform/core/capabilities.py")
compiler = load("compiler_connector_activation", "scripts/compile-workspace.py")
deployer = load("deployer_connector_activation", "scripts/deploy-client-docker.py")


class ConnectorActivationGateTests(unittest.TestCase):
    def test_real_adapters_are_usable_but_unproven_without_live_credentials(self):
        for provider in ("google", "microsoft"):
            binding = {"environment": "dev", "connectors": {
                "calendar": {"provider": provider, "tier": 1, "token_ref": "secret://calendar/dev"}
            }}
            self.assertEqual(capabilities.bound_providers(binding)["calendar"]["provider"], provider)
            compiled = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist",
                                                  environment_binding=binding)[0]
            self.assertEqual(compiled["capabilities"]["resolution"][0]["status"], "bound")
            manifest = capabilities.build_manifest(compiled, binding)
            calendar = next(x for x in manifest["capabilities"] if x["family"] == "calendar")
            self.assertEqual(calendar["health"], "unknown")
            self.assertNotIn("secret://", json.dumps(manifest))

    def test_planned_or_unregistered_provider_cannot_unlock_required_skill(self):
        for provider in ("hubspot", "jira", "not-yet-built"):
            binding = {"environment": "dev", "connectors": {
                "calendar": {"provider": provider, "tier": 1, "token_ref": "secret://calendar/dev"}
            }}
            self.assertNotIn("calendar", capabilities.bound_providers(binding))
            with self.assertRaisesRegex(ValueError, "book-appointment"):
                compiler.compile_workspace(ROOT / "examples/workspace", "receptionist",
                                           environment_binding=binding)
            report = compiler.workspace_readiness(ROOT / "examples/workspace",
                                                  "receptionist", (), binding)
            book = next(x for x in report[0]["skills"] if x["name"] == "book-appointment")
            self.assertFalse(book["ready"])
            self.assertEqual(book["dependencies"][0]["status"], "missing")

    def test_correct_provider_wrong_tier_does_not_unlock_skill(self):
        for tier in (0, 2, 3, 4):
            binding = {"environment": "dev", "connectors": {
                "calendar": {"provider": "google", "tier": tier}
            }}
            self.assertNotIn("calendar", capabilities.bound_providers(binding))
            with self.assertRaisesRegex(ValueError, "required capability|requires capability"):
                capabilities.resolve({"required": ["calendar"], "optional": []}, binding,
                                     agent_name="frontdesk")

    def test_tier_zero_future_crm_is_not_mistaken_for_an_implemented_baseline(self):
        binding = {"environment": "dev", "connectors": {
            "crm": {"provider": "built-in-crm", "tier": 0},
        }}
        self.assertEqual(capabilities.bound_providers(binding), {})
        skill = {"metadata": {"name": "log-call"}, "spec": {"requiredCapabilities": ["crm"]}}
        readiness = capabilities.skill_readiness({"metadata": {"name": "desk"}, "spec": {}},
                                                 [skill], binding)
        self.assertFalse(readiness[0]["ready"])
        self.assertEqual([x["status"] for x in readiness[0]["dependencies"]], ["missing"])
        with self.assertRaisesRegex(ValueError, "log-call"):
            capabilities.resolve({"required": ["crm"], "optional": []},
                                 binding, agent_name="desk",
                                 required_by=capabilities.skills_requiring([skill]))

    def test_tier_zero_text_knowledge_satisfies_kb_only_as_a_kb_provider_at_tier_zero(self):
        shipped = {"environment": "dev", "connectors": {"kb": {"provider": "text-knowledge", "tier": 0}}}
        self.assertEqual(capabilities.bound_providers(shipped)["kb"]["provider"], "text-knowledge")
        skill = {"metadata": {"name": "answer-faq"}, "spec": {"requiredCapabilities": ["kb"]}}
        readiness = capabilities.skill_readiness({"metadata": {"name": "desk"}, "spec": {}}, [skill], shipped)
        self.assertTrue(readiness[0]["ready"])
        self.assertEqual(readiness[0]["dependencies"][0]["tier"], 0)
        # A forged shape is still refused: wrong tier, wrong family, or a different unshipped provider.
        for connectors in (
            {"kb": {"provider": "text-knowledge", "tier": 1}},
            {"crm": {"provider": "text-knowledge", "tier": 0}},
            {"kb": {"provider": "confluence-knowledge", "tier": 2}},
            {"kb": {"provider": "notion-knowledge", "tier": 2}},
        ):
            self.assertEqual(capabilities.bound_providers({"environment": "dev", "connectors": connectors}), {}, connectors)

    def test_optional_unshipped_provider_degrades_instead_of_promising_connectivity(self):
        binding = {"environment": "dev", "connectors": {
            "tickets": {"provider": "jira", "tier": 2},
        }}
        skill = {"metadata": {"name": "take-message"},
                 "spec": {"optionalCapabilities": ["tickets"]}}
        report = capabilities.skill_readiness({"metadata": {"name": "desk"}, "spec": {}},
                                              [skill], binding)
        self.assertTrue(report[0]["ready"])
        self.assertEqual(report[0]["dependencies"][0]["status"], "degraded")
        manifest = capabilities.build_manifest({
            "source": {"name": "desk"}, "skills": [skill],
            "capabilities": {"optional": ["tickets"], "skills": report}
        }, binding)
        tickets = next(x for x in manifest["capabilities"] if x["family"] == "tickets")
        self.assertNotEqual(tickets["status"], "available")
        self.assertEqual(tickets["fallback"], "human-handoff")

    def test_deployment_rejects_fake_binding_even_if_compilation_was_bypassed(self):
        desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
        fake = {"environment": "dev", "connectors": {
            "calendar": {"provider": "not-real", "tier": 1},
        }}
        with self.assertRaisesRegex(ValueError, "book-appointment"):
            deployer._capability_manifest(desired, fake)
        self.assertIn(pathlib.Path("contracts/v1/integrations.json"), deployer.RUNTIME_SOURCE_PATHS)

    def test_registry_absence_fails_closed_rather_than_allowing_arbitrary_bindings(self):
        with mock.patch.object(capabilities, "INTEGRATIONS_PATH",
                               ROOT / "contracts/v1/does-not-exist.json"):
            with self.assertRaisesRegex(ValueError, "registry missing or invalid"):
                capabilities.bound_providers({"connectors": {
                    "calendar": {"provider": "google", "tier": 1}
                }})

    def test_legacy_calendar_binding_keeps_backward_compatibility(self):
        for key, expected in (("google_calendar", "google"), ("microsoft_calendar", "microsoft")):
            binding = {"connectors": {key: {"token_ref": "secret://calendar/dev"}}}
            self.assertEqual(capabilities.bound_providers(binding)["calendar"]["provider"], expected)


if __name__ == "__main__":
    unittest.main()
