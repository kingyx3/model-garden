"""Tenant-private MCP activation contract: owner consent, review, grants and revocation."""
from __future__ import annotations

import importlib.util
import contextlib
import io
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
operator = load("mcp_operator_under_test", "scripts/runtime-mcp-connections.py")
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

    def test_two_connections_of_one_family_resolve_their_own_grants(self):
        other = dict(SERVER)
        other["credentialRef"] = "secret://vendor/researcher-alt"
        second_grant = self.grants.create(
            agent="receptionist", family="crm", provider="vendor", connection_id="crm-alt",
            isolation="own-identity", acting_identity="research-alt@acme.example",
            secret_ref=other["credentialRef"], scopes=["crm.read", "crm.create"],
            owner="owner@acme.example", by="operator@acme.example", now=NOW,
        )
        self.store.stage("crm-alt", agent="receptionist", declaration=other,
                         by="operator@acme.example", now=NOW)
        alt = self.store.activate(
            "crm-alt", agent="receptionist", consented_by="owner@acme.example",
            by="reviewer@acme.example", offered=[READ, WRITE],
            approvals=self.approvals, grants=self.grants, now=NOW + 1,
        )
        self.assertTrue(alt["active"])
        self.activate()
        self.grants.revoke(self.grant["id"], by="operator@acme.example", now=NOW + 2)
        with self.assertRaises(PermissionError):
            self.grants.resolve(
                attested_agent="receptionist", family="crm", tool="crm.read",
                originator_permissions=["crm.read"], enabled_tools=["crm.read"], now=NOW + 3,
                connection_id="crm-main",
            )
        usable = self.grants.resolve(
            attested_agent="receptionist", family="crm", tool="crm.read",
            originator_permissions=["crm.read"], enabled_tools=["crm.read"], now=NOW + 3,
            connection_id="crm-alt",
        )
        self.assertEqual(usable["grantId"], second_grant["id"])
        self.assertTrue(self.store.resolve("crm-alt", agent="receptionist")["active"])

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

    def test_owner_cannot_self_approve_the_connection(self):
        with self.assertRaisesRegex(connections.ConnectionRefused, "separate"):
            self.activate(by="owner@acme.example")
        self.assertFalse(self.store.list_public(agent="receptionist")[0]["active"])

    def test_disable_during_schema_review_cannot_reactivate(self):
        original = self.approvals.status
        tripped = [False]

        def status(server, tool):
            if not tripped[0]:
                tripped[0] = True
                self.store.disable("crm-main", agent="receptionist", by="owner@acme.example")
            return original(server, tool)

        self.approvals.status = status
        with self.assertRaisesRegex(connections.ConnectionRefused, "changed during review"):
            self.activate()
        self.assertFalse(self.store.list_public(agent="receptionist")[0]["active"])
        with self.assertRaises(connections.ConnectionRefused):
            self.store.resolve("crm-main", agent="receptionist")

    def test_same_digest_replacement_during_review_cannot_approve_stale_snapshot(self):
        original = self.approvals.status
        tripped = [False]

        def status(server, tool):
            if not tripped[0]:
                tripped[0] = True
                # Identical manifest hashes must not defeat state-transition fencing.
                self.stage(replace=True)
            return original(server, tool)

        self.approvals.status = status
        with self.assertRaisesRegex(connections.ConnectionRefused, "changed during review"):
            self.activate()
        self.assertFalse(self.store.list_public(agent="receptionist")[0]["active"])

    def test_grant_revoked_after_review_is_refused_at_commit(self):
        original = self.approvals.status
        tripped = [False]

        def status(server, tool):
            if not tripped[0]:
                tripped[0] = True
                self.grants.revoke(self.grant["id"], by="operator@acme.example", now=NOW + 2)
            return original(server, tool)

        self.approvals.status = status
        with self.assertRaises(PermissionError):
            self.activate()
        self.assertFalse(self.store.list_public(agent="receptionist")[0]["active"])

    def test_double_activation_must_be_replaced_and_reconsented(self):
        self.activate()
        first = self.store.resolve("crm-main", agent="receptionist")
        with self.assertRaisesRegex(connections.ConnectionRefused, "already active"):
            self.activate()
        self.assertEqual(self.store.resolve("crm-main", agent="receptionist")["activationId"], first["activationId"])
        self.store.disable("crm-main", agent="receptionist", by="owner@acme.example")
        with self.assertRaises(connections.ConnectionRefused):
            self.store.resolve("crm-main", agent="receptionist")
        self.activate()
        second = self.store.resolve("crm-main", agent="receptionist")
        self.assertNotEqual(first["activationId"], second["activationId"])

    def test_activation_succeeds_with_valid_grants_and_revision_fence(self):
        self.assertFalse(self.store.list_public(agent="receptionist")[0]["active"])
        self.activate()
        record = self.store.resolve("crm-main", agent="receptionist")
        self.assertGreaterEqual(record["generation"], 2)
        self.assertNotIn("secret://", json.dumps(self.store.list_public(agent="receptionist")))

    def test_operator_cli_stage_status_and_disable_are_scoped_and_ledgered(self):
        manifest = self.root / "declaration.json"
        manifest.write_text(json.dumps(SERVER))
        data_root = self.root / "operator-root"
        def call(*arguments):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = operator.main([str(data_root), "--tenant", "acme-prod", *arguments])
            return result, output.getvalue()
        code, staged = call("stage", "--agent", "receptionist", "--id", "crm-main",
                            "--file", str(manifest), "--actor", "operator@acme.example")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(staged)["active"])
        self.assertNotIn("secret://", staged)
        code, status = call("status", "--agent", "receptionist")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(status)["connections"]), 1)
        self.assertNotIn("secret://", status)
        code, disabled = call("disable", "--agent", "receptionist", "--id", "crm-main",
                              "--actor", "owner@acme.example")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(disabled)["active"])
        ledger = (data_root / "audit.jsonl").read_text()
        self.assertIn("mcp.connection-stage", ledger)
        self.assertIn("mcp.connection-disable", ledger)
        self.assertNotIn("secret://", ledger)
        self.assertNotIn("actual-token", ledger)

    def test_operator_cli_rejects_secret_values_and_symlink_manifests(self):
        root = self.root / "cli-root"
        filename = self.root / "bad.json"
        wrong = {**SERVER, "credentialRef": "Bearer actual-token"}
        filename.write_text(json.dumps(wrong))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = operator.main([str(root), "--tenant", "acme-prod", "stage", "--agent", "receptionist",
                                    "--id", "crm-main", "--file", str(filename),
                                    "--actor", "owner@acme.example"])
        self.assertEqual(result, 2)
        self.assertNotIn("actual-token", stderr.getvalue())
        self.assertFalse((root / "mcp-connections" / "acme-prod.mcp-connections.json").exists())
        filename.write_text(json.dumps(SERVER))
        link = self.root / "manifest-symlink.json"
        link.symlink_to(filename)
        with contextlib.redirect_stderr(io.StringIO()):
            result = operator.main([str(root), "--tenant", "acme-prod", "stage", "--agent", "receptionist",
                                    "--id", "crm-main", "--file", str(link),
                                    "--actor", "owner@acme.example"])
        self.assertEqual(result, 2)

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
