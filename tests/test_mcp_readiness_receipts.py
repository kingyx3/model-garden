"""Regression tests for code-closeable MCP readiness, error and receipt handling.

No external MCP host or customer credentials are needed; this is not provider-live proof.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(key, relative):
    spec = importlib.util.spec_from_file_location(key, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


gateway = load("readiness_test_gateway", "platform/core/mcp_executor.py")
connections = load("readiness_test_connections", "platform/core/mcp_connections.py")
grants_module = load("readiness_test_grants", "platform/core/provider_grants.py")
mcp = load("readiness_test_mcp", "platform/core/mcp_passthrough.py")
operator = load("readiness_test_operator", "scripts/runtime-mcp-connections.py")
NOW = 1_800_000_000
SERVER = {
    "name": "crm-provider",
    "url": "https://mcp.crm-provider.example/api",
    "credentialRef": "secret://provider/receptionist",
    "owner": "owner@acme.example",
    "tools": {"search": {"capability": "crm.read", "risk": "read"}},
}
SEARCH = {"name": "search", "description": "Search customer records",
          "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                          "required": ["query"], "additionalProperties": False}}


class Backend:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, url, pinned, secret):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def list_tools(self):
        return [SEARCH]

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.result


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.store = connections.McpConnectionStore(self.root / "mcp-connections", tenant="acme-dev")
        self.grants = grants_module.GrantRegistry(self.root / "provider-grants", tenant="acme-dev")
        self.approvals = mcp.SchemaApprovals(self.root / "mcp-schema-approvals", tenant="acme-dev")
        self.store.stage("crm-primary", agent="receptionist", declaration=SERVER, by="operator@acme.example", now=NOW)

    def grant(self, agent="receptionist", conn="crm-primary", secret=None, identity=None):
        return self.grants.create(
            agent=agent, family="crm", provider="crm-provider", connection_id=conn,
            isolation="own-identity", acting_identity=identity or f"{agent}@acme.example",
            secret_ref=secret or f"secret://provider/{agent}", scopes=["crm.read"],
            owner="owner@acme.example", by="operator@acme.example", now=NOW,
        )

    def status(self, agent="receptionist"):
        return self.store.list_readiness(agent=agent, grants=self.grants, now=NOW + 1)

    def activate(self):
        self.approvals.approve("crm-provider", SEARCH, by="reviewer@platform.example", now=NOW)
        return self.store.activate(
            "crm-primary", agent="receptionist", consented_by="owner@acme.example",
            by="operator@acme.example", offered=[SEARCH], approvals=self.approvals,
            grants=self.grants, now=NOW + 1,
        )

    def test_inactive_is_not_reported_usable_without_a_grant(self):
        item = self.status()[0]
        self.assertEqual(item["status"], "inactive")
        self.assertFalse(item["tools"][0]["grantReady"])
        self.assertFalse(item["active"])
        self.assertNotIn("secret://", json.dumps(item))
        self.assertNotIn(SERVER["url"], json.dumps(item))

    def test_own_grant_changes_readiness_but_does_not_activate_connection(self):
        self.grant()
        item = self.status()[0]
        self.assertEqual(item["status"], "inactive")
        self.assertTrue(item["tools"][0]["grantReady"])
        self.activate()
        item = self.status()[0]
        self.assertEqual(item["status"], "configured-awaiting-upstream-proof")
        self.assertEqual(item["tools"], [{"tool": "crm.read", "risk": "read", "grantReady": True}])
        self.assertNotIn("activationId", json.dumps(item))
        self.assertNotIn("actingIdentity", json.dumps(item))

    def test_revocation_and_health_failure_are_attributed_to_the_one_coworker(self):
        first = self.grant()
        self.activate()
        self.store.stage("crm-secondary", agent="backoffice", declaration={
            **SERVER, "credentialRef": "secret://provider/backoffice"},
            by="operator@acme.example", now=NOW,
        )
        self.grant(agent="backoffice", conn="crm-secondary")
        self.assertTrue(self.status("backoffice")[0]["tools"][0]["grantReady"])
        self.grants.revoke(first["id"], by="operator@acme.example", now=NOW + 2)
        item = self.status()[0]
        self.assertEqual(item["status"], "grant-unavailable")
        self.assertFalse(item["tools"][0]["grantReady"])
        self.assertTrue(self.status("backoffice")[0]["tools"][0]["grantReady"])

    def test_wrong_tenant_registry_and_disabled_route(self):
        other = grants_module.GrantRegistry(self.root / "wrong", tenant="other-prod")
        with self.assertRaises(connections.ConnectionRefused):
            self.store.list_readiness(agent="receptionist", grants=other, now=NOW)
        self.grant()
        self.activate()
        self.store.disable("crm-primary", agent="receptionist", by="operator@acme.example")
        self.assertEqual(self.status()[0]["status"], "inactive")

    def test_operator_status_with_grants_redacts_all_private_data(self):
        self.grant()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = operator.main([
                str(self.root), "--tenant", "acme-dev", "status", "--agent", "receptionist", "--check-grants"
            ])
        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["connections"][0]["tools"][0]["grantReady"])
        for hidden in (SERVER["url"], SERVER["credentialRef"], "receptionist@acme.example", "activationId"):
            self.assertNotIn(hidden, output.getvalue())


class UpstreamResultTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.registry = grants_module.GrantRegistry(self.root / "grants", tenant="acme-dev")
        self.registry.create(agent="receptionist", family="crm", provider="crm-provider",
            connection_id="crm-primary", isolation="own-identity",
            acting_identity="receptionist@acme.example", secret_ref=SERVER["credentialRef"],
            scopes=["crm.read"], owner="owner@acme.example", by="operator@acme.example", now=NOW)
        self.approvals = mcp.SchemaApprovals(self.root / "pins", tenant="acme-dev")
        self.approvals.approve("crm-provider", SEARCH, by="reviewer@platform.example", now=NOW)
        self.route = self.prepare()

    def prepare(self):
        return gateway.prepare(SERVER, registry=self.registry, tenant="acme-dev",
            attested_agent="receptionist", upstream="search", enabled_tools=["crm.read"],
            originator_permissions=["crm.read"], connection_id="crm-primary", now=NOW + 1)

    def call(self, result):
        backend = Backend(result)
        def secret_resolve(_):
            return "secret-used-for-test-only"
        return gateway.invoke(
            self.route, {"query": "Alice"}, approvals=self.approvals, transport_factory=backend,
            secret_resolver=secret_resolve, dns_resolver=lambda _: ["93.184.216.34"], recheck=self.prepare,
        )

    def test_upstream_tool_error_is_not_misreported_as_a_success(self):
        with self.assertRaisesRegex(gateway.UpstreamToolError, "reported an error") as caught:
            self.call({"isError": True, "content": [{"text": "Bearer secret-used-for-test-only"}]})
        self.assertNotIn("Bearer", str(caught.exception))
        self.assertNotIn("secret-used-for-test-only", str(caught.exception))

    def test_false_error_flag_and_valid_business_result_are_accepted(self):
        self.assertEqual(self.call({"isError": False, "objectId": "c-123"})["objectId"], "c-123")

    def test_ambiguous_error_flag_is_not_interpreted_as_success(self):
        for value in ("false", "true", 1, None, {}, []):
            with self.assertRaises(gateway.GatewayError, msg=str(value)):
                self.call({"isError": value, "objectId": "c-123"})

    def test_audit_receipt_only_allows_short_opaque_nonsecret_id(self):
        for good in ("crm:contact-123", "object/abc-12", "ABC_201", "urn:acme:record:123"):
            self.assertEqual(gateway.receipt_reference({"externalObjectRef": good}), good)
        for bad in (
            "Bearer my-secret", "https://api.example/?token=secret", "a@b.com",
            "../../secret", "", 42, {"password": "secret"}, "x" * 129, "contact\nforgery",
        ):
            self.assertIsNone(gateway.receipt_reference({"externalObjectRef": bad}))
        self.assertIsNone(gateway.receipt_reference({}))


if __name__ == "__main__":
    unittest.main()
