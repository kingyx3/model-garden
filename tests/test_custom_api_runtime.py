"""A client's own REST API through the governed sidecar (#159, #89): declaration agreement, the REST session and the runtime path.

Everything upstream is a fake connection object; nothing here is a real provider or credential, and no provider status changes.
"""
from __future__ import annotations

import base64
import copy
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mcp = load("mcp_for_custom_api_runtime", "platform/core/mcp_passthrough.py")
custom_api = load("custom_api_for_runtime", "platform/core/custom_api.py")
grants = load("grants_for_custom_api_runtime", "platform/core/provider_grants.py")
upstream_http = load("upstream_http_for_custom_api", "platform/runtime/upstream_http.py")
compiler = load("compiler_for_custom_api_runtime", "scripts/compile-workspace.py")
runtime_module = load("runtime_for_custom_api", "platform/runtime/governed_mcp.py")
approval = load("approval_for_custom_api_runtime", "scripts/runtime-approval.py")

NOW = 1_800_000_001
SECRET_REF = "secret://acme/orders/receptionist"
SECRET = "fake-orders-key-not-for-production"
PINNED = "93.184.216.34"
API = {
    "name": "orders", "baseUrl": "https://api.orders.example/v2", "owner": "owner@acme.example",
    "auth": {"type": "bearer", "credentialRef": SECRET_REF},
    "operations": {
        "create-customer": {"method": "POST", "path": "/customers", "summary": "Create a customer record",
                            "body": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        "log-call": {"method": "POST", "path": "/customers/{customerId}/notes", "summary": "Add a call note to a customer",
                     "parameters": {"customerId": {"in": "path", "required": True, "schema": {"type": "string"}}},
                     "body": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        "find-customer": {"method": "GET", "path": "/customers", "summary": "Find customers by name",
                          "parameters": {"q": {"in": "query", "required": True, "schema": {"type": "string"}}}},
    },
}
DECLARATION = {
    "name": "orders", "url": "https://api.orders.example/v2", "credentialRef": SECRET_REF, "owner": "owner@acme.example",
    "tools": {"create-customer": {"capability": "crm.create", "risk": "write"},
              "log-call": {"capability": "crm.log-interaction", "risk": "write"},
              "find-customer": {"capability": "crm.read", "risk": "read"}},
    "api": API,
}


class FakeResponse:
    def __init__(self, status, body=b"", content_type="application/json"):
        self.status, self._body, self._type = status, body, content_type

    def read(self, limit):
        return self._body[:limit]

    def getheader(self, name):
        return self._type if name == "Content-Type" else None


class FakeApi:
    """Records every connection and request; answers with the next queued response."""

    def __init__(self):
        self.dialed, self.requests = [], []
        self.responses = []

    def connect(self, host, ip, port, timeout):
        self.dialed.append((host, ip, port, timeout))
        return self

    def request(self, method, target, body=None, headers=None):
        self.requests.append({"method": method, "target": target, "body": json.loads(body) if body else None, "headers": dict(headers or {})})

    def getresponse(self):
        return self.responses.pop(0) if self.responses else FakeResponse(200, json.dumps({"id": "cust-42"}).encode())

    def close(self):
        pass


def approved(root):
    approvals = mcp.SchemaApprovals(root, tenant="acme-dev")
    for tool in custom_api.operation_tools(API):
        approvals.approve("orders", tool, by="reviewer@platform.example", now=NOW)
    return approvals


class DeclarationCase(unittest.TestCase):
    def test_an_api_declaration_must_agree_with_its_definition(self):
        server = mcp.validate_server(DECLARATION)
        self.assertEqual(server["api"]["name"], "orders")
        for change, message in (
            ({"name": "other"}, "name"),
            ({"url": "https://api.orders.example/v3"}, "base URL"),
            ({"credentialRef": "secret://acme/orders/other"}, "credential"),
            ({"tools": {"create-customer": {"capability": "crm.create", "risk": "write"}}}, "exactly"),
            ({"tools": {**DECLARATION["tools"], "find-customer": {"capability": "crm.read", "risk": "write"}}}, "risk"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(mcp.McpDeclarationError, message):
                mcp.validate_server({**DECLARATION, **change})
        unauthenticated = copy.deepcopy(DECLARATION)
        unauthenticated["api"]["auth"] = {"type": "none"}
        with self.assertRaisesRegex(mcp.McpDeclarationError, "credential"):
            mcp.validate_server(unauthenticated)
        private = copy.deepcopy(DECLARATION)
        private["api"]["baseUrl"] = private["url"] = "https://10.0.0.5/v2"
        with self.assertRaises(mcp.McpDeclarationError):
            mcp.validate_server(private)

    def test_operation_tools_are_plain_json_schema_of_the_arguments(self):
        tool = next(item for item in custom_api.operation_tools(API) if item["name"] == "log-call")
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], ["body", "customerId"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual((schema["x-method"], schema["x-path"], schema["x-in"]), ("POST", "/customers/{customerId}/notes", {"customerId": "path"}))


class SessionCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.approvals = approved(pathlib.Path(temp.name))
        self.api = FakeApi()

    def session(self, definition=API, credential=SECRET):
        return upstream_http.RestApiSession(definition, PINNED, credential, self.approvals, connect=self.api.connect)

    def test_lists_only_declared_operations_and_sends_only_the_built_request(self):
        with self.session() as session:
            self.assertEqual(sorted(t["name"] for t in session.list_tools()), ["create-customer", "find-customer", "log-call"])
            found = session.call_tool("find-customer", {"q": "Alex Tan"})
            noted = session.call_tool("log-call", {"customerId": "cust-42", "body": {"text": "Called back"}})
        self.assertEqual(self.api.dialed[0], ("api.orders.example", PINNED, 443, 15.0))
        self.assertEqual(self.api.requests[0]["method"], "GET")
        self.assertEqual(self.api.requests[0]["target"], "/v2/customers?q=Alex+Tan")
        self.assertEqual(self.api.requests[0]["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(self.api.requests[1]["target"], "/v2/customers/cust-42/notes")
        self.assertEqual(self.api.requests[1]["body"], {"text": "Called back"})
        self.assertEqual(found["externalObjectRef"], "cust-42")
        self.assertFalse(noted["isError"])

    def test_header_and_basic_authentication(self):
        header = copy.deepcopy(API)
        header["auth"] = {"type": "header", "header": "X-Api-Key", "credentialRef": SECRET_REF}
        basic = copy.deepcopy(API)
        basic["auth"] = {"type": "basic", "credentialRef": SECRET_REF}
        for definition, credential in ((header, SECRET), (basic, "user:pass")):
            for tool in custom_api.operation_tools(definition):  # a different auth type is a different pin, approved again
                self.approvals.approve("orders", tool, by="reviewer@platform.example", now=NOW)
            self.session(definition, credential).call_tool("find-customer", {"q": "a"})
        self.assertEqual(self.api.requests[0]["headers"]["X-Api-Key"], SECRET)
        self.assertNotIn("Authorization", self.api.requests[0]["headers"])
        self.assertEqual(self.api.requests[1]["headers"]["Authorization"], "Basic " + base64.b64encode(b"user:pass").decode())

    def test_unapproved_or_undeclared_calls_never_leave(self):
        with self.assertRaises(upstream_http.UpstreamTransportError):
            self.session().call_tool("delete-everything", {})
        with self.assertRaises(upstream_http.UpstreamTransportError):
            self.session().call_tool("log-call", {"customerId": "../admin", "body": {"text": "x"}})
        changed = copy.deepcopy(API)
        changed["operations"]["find-customer"]["summary"] = "Find customers by name or email"
        with self.assertRaises(upstream_http.UpstreamTransportError):
            self.session(changed).call_tool("find-customer", {"q": "a"})
        self.assertEqual(self.api.requests, [])

    def test_failures_are_bounded_and_never_echo_upstream_text_or_the_credential(self):
        leak = json.dumps({"error": SECRET}).encode()
        cases = [(FakeResponse(302, leak), "redirect"), (FakeResponse(401, leak), "(401)"), (FakeResponse(503, leak), "503"),
                 (FakeResponse(200, b"<html>" + SECRET.encode(), "text/html"), "not JSON"),
                 (FakeResponse(200, b"x" * (upstream_http.MAX_RESPONSE_BYTES + 1)), "too large")]
        for response, message in cases:
            self.api.responses = [response]
            with self.subTest(message=message), self.assertRaises(upstream_http.UpstreamTransportError) as caught:
                self.session().call_tool("find-customer", {"q": "a"})
            self.assertIn(message, str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))
        self.api.responses = [FakeResponse(404, leak)]
        rejected = self.session().call_tool("find-customer", {"q": "a"})
        self.assertEqual(rejected, {"isError": True, "status": 404})

    def test_install_adds_the_rest_transport_only_with_upstream_enabled(self):
        self.assertIsNone(upstream_http.install({}))
        installed = upstream_http.install({"MODEL_GARDEN_UPSTREAM_MCP": "enabled"})
        self.assertIs(installed["upstream_rest_transport"], upstream_http.rest_transport_factory)


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.api = FakeApi()
        self.mcp_calls = []

    def runtime(self, *, rest=True):
        workspace = self.root / "workspace"
        shutil.copytree(ROOT / "examples/workspace", workspace)
        desired = compiler.compile_workspace(workspace, "receptionist")[0]
        desired["source"]["client"] = "acme"
        for tool in desired["tools"]:
            if tool["metadata"]["name"] in ("crm.create", "crm.log-interaction"):
                tool["spec"]["type"] = "mcp"
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired))
        manifest = {"client": "acme", "environment": "dev",
                    "capabilities": [{"family": "crm", "provider": "orders", "status": "available", "tier": 2}]}
        rt = runtime_module.GovernedToolRuntime(
            path, self.root / "audit.jsonl", self.root / "runtime-approvals", environ={},
            upstream_transport=lambda *a: self.mcp_calls.append(a),
            upstream_dns_resolver=lambda host: [PINNED] if host == "api.orders.example" else [],
            upstream_secret_resolver=lambda ref: SECRET if ref == SECRET_REF else "",
            upstream_rest_transport=(lambda api, ip, cred, approvals: upstream_http.RestApiSession(api, ip, cred, approvals, connect=self.api.connect)) if rest else None,
        )
        rt.capabilities = lambda: manifest  # fixture stands in for vetted registry evidence; production cannot infer "available"
        rt.governance_root.mkdir(exist_ok=True)
        self.registry = grants.GrantRegistry(rt.governance_root / "provider-grants", tenant="acme-dev")
        self.grant = self.registry.create(
            agent="receptionist", family="crm", provider="orders", connection_id="orders-api", isolation="own-identity",
            acting_identity="receptionist-bot@acme.example", secret_ref=SECRET_REF, scopes=["crm.create", "crm.log-interaction", "crm.read"],
            owner="owner@acme.example", by="operator@acme.example", now=NOW,
        )
        approvals = approved(rt.governance_root / "mcp-schema-approvals")
        store = runtime_module.MCP_CONNECTIONS.McpConnectionStore(rt.governance_root / "mcp-connections", tenant="acme-dev")
        store.stage("orders-api", agent="receptionist", declaration=DECLARATION, by="operator@acme.example", now=NOW)
        store.activate("orders-api", agent="receptionist", consented_by="owner@acme.example", by="operator@acme.example",
                       offered=custom_api.operation_tools(API), approvals=approvals, grants=self.registry, now=NOW + 1)
        self.store = store
        return rt

    def test_writes_wait_for_exact_approval_then_go_to_the_pinned_api_with_coworker_attribution(self):
        rt = self.runtime()
        args = {"body": {"name": "Alex Tan"}}
        pending = rt.upstream_call("orders-api", "create-customer", args)
        self.assertEqual(pending["status"], "pending-approval")
        self.assertEqual(self.api.requests, [])
        approval.decide(rt.approval_root, pending["request"]["requestId"], approved=True, approver="owner@acme.example")
        done = rt.upstream_call("orders-api", "create-customer", args)
        self.assertEqual(done["status"], "executed")
        self.assertEqual([(r["method"], r["target"], r["body"]) for r in self.api.requests], [("POST", "/v2/customers", {"name": "Alex Tan"})])
        self.assertEqual(self.api.dialed[0][1], PINNED)
        self.assertEqual(self.mcp_calls, [])
        audit = rt.audit_path.read_text()
        self.assertIn('"provider":"api:orders"', audit)
        self.assertIn(self.grant["id"], audit)
        self.assertIn("receptionist-bot@acme.example", audit)
        self.assertIn("cust-42", audit)
        self.assertNotIn(SECRET, audit)
        self.assertNotIn(SECRET_REF, audit)
        self.assertNotIn("Alex Tan", audit)

    def test_policy_allowed_write_runs_and_invalid_arguments_never_leave(self):
        rt = self.runtime()
        done = rt.upstream_call("orders-api", "log-call", {"customerId": "cust-42", "body": {"text": "Called back"}})
        self.assertEqual(done["status"], "executed")
        with self.assertRaises(Exception):
            rt.upstream_call("orders-api", "log-call", {"customerId": "cust-42", "body": {"text": "x"}, "url": "https://evil.example"})
        self.assertEqual(len(self.api.requests), 1)

    def test_disable_or_revoke_stops_the_api_and_nothing_runs_without_the_rest_transport(self):
        rt = self.runtime()
        self.assertEqual([t["tool"] for t in rt.upstream_tools()], ["crm.create", "crm.log-interaction"])
        self.registry.revoke(self.grant["id"], by="owner@acme.example", now=NOW + 5)
        with self.assertRaises(PermissionError):
            rt.upstream_call("orders-api", "log-call", {"customerId": "cust-42", "body": {"text": "x"}})
        self.store.disable("orders-api", agent="receptionist", by="operator@acme.example")
        self.assertEqual(rt.upstream_tools(), [])
        bare = RuntimeCase.runtime.__get__(self)
        self.root = self.root / "second"
        self.root.mkdir()
        no_rest = bare(rest=False)
        self.assertEqual(no_rest.upstream_tools(), [])
        with self.assertRaisesRegex(PermissionError, "REST API transport"):
            no_rest.upstream_call("orders-api", "log-call", {"customerId": "cust-42", "body": {"text": "x"}})
        self.assertEqual(self.api.requests, [])


if __name__ == "__main__":
    unittest.main()
