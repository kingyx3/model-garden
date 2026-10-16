"""Tenant-private MCP activation contract: owner consent, review, grants and revocation."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


connections = load("mcp_connections_under_test", "platform/core/mcp_connections.py")
mcp = load("mcp_approvals_for_connection_tests", "platform/core/mcp_passthrough.py")
grants_module = load("mcp_grants_for_connection_tests", "platform/core/provider_grants.py")
NOW = 1_800_000_000

SERVER = {"name": "vendor", "url": "https://api.vendor.example/mcp",
          "credentialRef": "secret://vendor/acme-prod", "owner": "owner@acme.example",
          "tools": {"search_contacts": {"capability": "crm.read", "risk": "read"},
                    "create_contact": {"capability": "crm.create", "risk": "write"}}}
READ = {"name": "search_contacts", "description": "Search contacts",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}
WRITE = {"name": "create_contact", "description": "Create contacts",
         "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}}


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.events = []
        self.store = connections.McpConnectionStore(
            self.root / "connections", tenant="acme-prod", audit=lambda *event: self.events.append(event)
        )
        self.approvals = mcp.SchemaApprovals(self.root / "approval", tenant="acme-prod")
        self.grants = grants_module.GrantRegistry(self.root / "grants", tenant="acme-prod")
        self.grant = self.grants.create(
            agent="receptionist", family="crm", provider="vendor", connection_id="crm-main",
            isolation="own-identity", acting_identity="receptionist@acme.example",
            secret_ref=SERVER["credentialRef"], scopes=["crm.read", "crm.create"],
            owner="owner@acme.example", by="operator@acme.example", now=NOW,
        )
        self.stage()
        self.pin()

    def stage(self, **overrides):
        return self.store.stage("crm-main", agent="receptionist", declaration=SERVER,
                                by="operator@acme.example", now=NOW, **overrides)

    def pin(self):
        for tool in (READ, WRITE):
            self.approvals.approve("vendor", tool, by="reviewer@acme.example", now=NOW)

    def activate(self, **overrides):
        args = {"connection_id": "crm-main", "agent": "receptionist",
                "consented_by": "owner@acme.example", "by": "reviewer@acme.example",
                "offered": [READ, WRITE], "approvals": self.approvals, "grants": self.grants, "now": NOW + 1}
        args.update(overrides)
        return self.store.activate(**args)

    def test_staging_cannot_execute_and_persists_without_secret_material(self):
        with self.assertRaises(connections.ConnectionRefused):
            self.store.resolve("crm-main", agent="receptionist")
        stored = self.store.path.read_text()
        self.assertIn("secret://vendor/acme-prod", stored)
        self.assertNotIn("actual-token", stored)
        self.assertNotIn("actual-token", json.dumps(self.events))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.list_public(agent="receptionist")[0]["active"], False)
        self.assertNotIn("secret://", json.dumps(self.store.list_public(agent="receptionist")))

    def test_consent_approved_pins_and_grants_activate_across_restart(self):
        activated = self.activate()
        self.assertTrue(activated["active"])
        restarted = connections.McpConnectionStore(self.root / "connections", tenant="acme-prod")
        stored = restarted.resolve("crm-main", agent="receptionist")
        self.assertEqual(stored["reviewedFingerprints"]["create_contact"], mcp.fingerprint(WRITE))
        self.assertEqual(stored["declaration"]["url"], SERVER["url"])
        self.assertEqual(stored["consentedBy"], "owner@acme.example")
        self.assertNotIn("secret://", json.dumps(self.events))
        self.assertEqual(len([event for event in self.events if event[0] == "mcp.connection-activate"]), 1)

    def test_owner_consent_and_pinned_schema_are_mandatory(self):
        with self.assertRaises(connections.ConnectionRefused):
            self.activate(consented_by="outsider@acme.example")
        with self.assertRaises(PermissionError):
            self.activate(offered=[READ])
        with self.assertRaises(PermissionError):
            self.activate(offered=[READ, {**WRITE, "description": "Changed after review"}])
        self.assertFalse(self.store.list_public(agent="receptionist")[0]["active"])

    def test_wrong_tenant_agent_grant_connection_and_scope_are_refused(self):
        with self.assertRaises(connections.ConnectionRefused):
            self.activate(grants=grants_module.GrantRegistry(self.root / "other", tenant="other-prod"))
        with self.assertRaises(connections.ConnectionRefused):
            self.activate(agent="other-coworker")
        self.grants.revoke(self.grant["id"], by="operator@acme.example", now=NOW + 2)
        with self.assertRaises(PermissionError):
            self.activate()
        self.grants.create(
            agent="receptionist", family="crm", provider="vendor", connection_id="wrong-connection",
            isolation="own-identity", acting_identity="another@acme.example",
            secret_ref="secret://vendor/another", scopes=["crm.read", "crm.create"],
            owner="owner@acme.example", by="operator@acme.example", now=NOW + 3,
        )
        with self.assertRaises(PermissionError):
            self.activate()

    def test_disabling_cuts_access_on_next_read_and_survives_restart(self):
        self.activate()
        self.store.disable("crm-main", agent="receptionist", by="owner@acme.example")
        restarted = connections.McpConnectionStore(self.root / "connections", tenant="acme-prod")
        with self.assertRaises(connections.ConnectionRefused):
            restarted.resolve("crm-main", agent="receptionist")
        self.assertFalse(restarted.list_public(agent="receptionist")[0]["active"])

    def test_replacement_forces_new_review_then_reactivation(self):
        self.activate()
        replacement = dict(SERVER)
        replacement["url"] = "https://mcp2.vendor.example/v2"
        snapshot = self.store.stage("crm-main", agent="receptionist", declaration=replacement,
                                    by="operator@acme.example", now=NOW + 3, replace=True)
        self.assertFalse(snapshot["active"])
        with self.assertRaises(connections.ConnectionRefused):
            self.store.resolve("crm-main", agent="receptionist")
        self.activate()
        self.assertEqual(self.store.resolve("crm-main", agent="receptionist")["declaration"]["url"], replacement["url"])

    def test_unknown_portable_tool_and_risk_or_credential_injection_are_denied(self):
        wrong = dict(SERVER)
        wrong["tools"] = {"raw_shell": {"risk": "privileged"}}
        with self.assertRaises(connections.ConnectionRefused):
            self.store.stage("other", agent="receptionist", declaration=wrong,
                             by="operator@acme.example", now=NOW)
        wrong["tools"] = {"search_contacts": {"capability": "crm.read", "risk": "privileged"}}
        with self.assertRaises(connections.ConnectionRefused):
            self.store.stage("other", agent="receptionist", declaration=wrong,
                             by="operator@acme.example", now=NOW)
        wrong["tools"] = SERVER["tools"]
        wrong["credentialRef"] = "Bearer supersecret"
        with self.assertRaises(ValueError):
            self.store.stage("other", agent="receptionist", declaration=wrong,
                             by="operator@acme.example", now=NOW)

    def test_cross_tenant_store_and_forged_file_are_denied(self):
        self.activate()
        another = connections.McpConnectionStore(self.root / "connections", tenant="other-prod")
        with self.assertRaises(connections.ConnectionRefused):
            another.resolve("crm-main", agent="receptionist")
        data = json.loads(self.store.path.read_text())
        data["tenant"] = "other-prod"
        self.store.path.write_text(json.dumps(data))
        with self.assertRaises(connections.ConnectionRefused):
            self.store.resolve("crm-main", agent="receptionist")

    def test_same_id_cannot_silently_rebind_or_claim_other_agent(self):
        with self.assertRaises(connections.ConnectionRefused):
            self.stage()
        with self.assertRaises(connections.ConnectionRefused):
            self.store.resolve("crm-main", agent="different-coworker")
        with self.assertRaises(connections.ConnectionRefused):
            self.store.stage("absent", agent="receptionist", declaration=SERVER,
                             by="operator@acme.example", now=NOW, replace=True)


if __name__ == "__main__":
    unittest.main()
