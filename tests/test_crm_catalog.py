from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


capabilities = _load("capabilities_crm_catalog", "platform/core/capabilities.py")
govern = _load("govern_action_crm_catalog", "scripts/govern-action.py")


class CrmCatalogTests(unittest.TestCase):
    def test_crm_family_exposes_portable_create_and_interaction_logging(self):
        crm = capabilities.families()["crm"]["tools"]
        self.assertEqual(set(crm), {"crm.read", "crm.update", "crm.create", "crm.log-interaction"})
        self.assertEqual(capabilities.tool_risk("crm.create"), "write")
        self.assertEqual(capabilities.tool_risk("crm.log-interaction"), "write")
        self.assertEqual(capabilities.family_of("crm.log-interaction"), "crm")

    def test_ledger_fields_are_declared_arguments_and_never_carry_content(self):
        for name in ("crm.create", "crm.log-interaction"):
            tool = capabilities.catalog_tool(name)
            self.assertTrue(set(tool["ledgerFields"]) <= set(tool["arguments"]["properties"]), name)
        fields = capabilities.ledger_fields("crm.log-interaction")
        self.assertEqual(fields, frozenset({"objectType", "objectId", "interactionType"}))
        for forbidden in ("summary", "transcriptRef", "fields", "outcome"):
            self.assertNotIn(forbidden, fields)
            self.assertNotIn(forbidden, capabilities.ledger_fields("crm.create"))

    def test_audit_redacts_summary_and_transcript_reference(self):
        redacted = govern.redact_arguments(
            "crm.log-interaction",
            {
                "objectType": "contact",
                "objectId": "contact-42",
                "interactionType": "call",
                "channel": "voice",
                "direction": "inbound",
                "timestamp": "2026-10-20T10:00:00+08:00",
                "summary": "Caller asked about a private matter",
                "transcriptRef": "transcripts/call-1",
            },
        )
        self.assertEqual((redacted["objectId"], redacted["interactionType"]), ("contact-42", "call"))
        for key in ("summary", "transcriptRef", "channel", "timestamp"):
            self.assertEqual(redacted[key], govern.REDACTED, key)

    def test_only_the_shipped_built_in_provider_claims_the_new_tools(self):
        registry = json.loads((ROOT / "contracts" / "v1" / "integrations.json").read_text())
        claimed = {
            provider["id"]
            for provider in registry["providers"]
            for tool in provider["tools"]
            if tool in ("crm.create", "crm.log-interaction")
        }
        # HubSpot, Salesforce and Zoho (#93) must not list them before their adapters and proof exist.
        self.assertEqual(claimed, {"built-in-crm"})
        built_in = next(provider for provider in registry["providers"] if provider["id"] == "built-in-crm")
        self.assertEqual(built_in["status"], "built-credential-free")
        self.assertEqual(built_in["adapter"], "platform/core/contacts.py")
        self.assertNotIn("crm.update", built_in["tools"])  # not implemented at Tier 0
        self.assertNotIn("proof", built_in)


if __name__ == "__main__":
    unittest.main()
