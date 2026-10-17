"""Credential-free checks for the concrete upstream MCP transport and its sidecar wiring (#87, #158, #159)."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import importlib.util
import json
import pathlib
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


http = load("upstream_http_under_test", "platform/runtime/upstream_http.py")
runtime_module = load("runtime_for_upstream_http_tests", "platform/runtime/governed_mcp.py")
compiler = load("compiler_for_upstream_http_tests", "scripts/compile-workspace.py")
grants = load("grants_for_upstream_http_tests", "platform/core/provider_grants.py")
mcp = runtime_module.MCP_EXECUTOR._mcp()
NOW = 1_800_000_001
SECRET = "this-is-a-fake-secret-not-for-production"
SECRET_REF = "secret://hubspot/receptionist"


class FakeConnection:
    """A stand-in for the pinned HTTPS connection that records requests and replays canned responses."""

    def __init__(self, script, log):
        self._script, self._log, self._current = script, log, None

    def request(self, method, path, body=None, headers=None):
        self._log.append((method, path, json.loads(body) if body else None, dict(headers or {})))
        message = self._log[-1][2] or {}
        self._current = self._script(method, message)

    def getresponse(self):
        status, headers, body = self._current
        outer = self

        class Response:
            def __init__(self):
                self.status = status

            def read(self, limit=None):
                return body if limit is None else body[:limit]

            def getheader(self, name):
                return {k.lower(): v for k, v in headers.items()}.get(name.lower())

        return Response()

    def close(self):
        pass


def json_reply(message, result, *, headers=None):
    return 200, {"Content-Type": "application/json", **(headers or {})}, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()


def script_for(tools, *, call_result=None, sse=False):
    def respond(method, message):
        if method == "DELETE":
            return 200, {}, b""
        kind = message.get("method")
        if kind == "initialize":
            return json_reply(message, {"protocolVersion": http.PROTOCOL_VERSION, "capabilities": {}}, headers={"Mcp-Session-Id": "sess-1"})
        if kind == "notifications/initialized":
            return 202, {}, b""
        if kind == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            page = {"tools": tools[:1], "nextCursor": "p2"} if cursor is None and len(tools) > 1 else {"tools": tools[1:] if cursor else tools}
            return json_reply(message, page)
        if kind == "tools/call":
            body = {"jsonrpc": "2.0", "id": message["id"], "result": call_result or {"content": [], "structuredContent": {"id": "c-123"}}}
            if sse:
                return 200, {"Content-Type": "text/event-stream"}, ("event: message\ndata: " + json.dumps(body) + "\n\n").encode()
            return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
        return 400, {}, b""

    return respond


def session_with(script, log):
    return http.McpHttpSession("https://mcp.example.test/api/mcp", "93.184.216.34", SECRET, connect=lambda *args: FakeConnection(script, log))


class McpSessionTests(unittest.TestCase):
    TOOLS = [{"name": "a", "inputSchema": {"type": "object"}}, {"name": "b", "inputSchema": {"type": "object"}}]

    def test_initialises_pages_the_tool_list_and_calls_with_a_bearer_and_session(self):
        log = []
        with session_with(script_for(self.TOOLS), log) as session:
            self.assertEqual([t["name"] for t in session.list_tools()], ["a", "b"])
            result = session.call_tool("a", {"query": "x"})
        self.assertEqual(result["externalObjectRef"], "c-123")
        methods = [(entry[0], (entry[2] or {}).get("method")) for entry in log]
        self.assertEqual(methods[:2], [("POST", "initialize"), ("POST", "notifications/initialized")])
        self.assertEqual(log[-1][0], "DELETE")
        for entry in log[:-1]:
            self.assertEqual(entry[3]["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(log[2][3]["Mcp-Session-Id"], "sess-1")

    def test_server_sent_event_replies_are_understood(self):
        with session_with(script_for(self.TOOLS, sse=True), []) as session:
            self.assertEqual(session.call_tool("a", {})["structuredContent"]["id"], "c-123")

    def test_only_a_safe_opaque_id_becomes_an_external_reference(self):
        for bad in ("Bearer abc def", "x@y.com", "a" * 300):
            with session_with(script_for(self.TOOLS, call_result={"structuredContent": {"id": bad}}), []) as session:
                self.assertNotIn("externalObjectRef", session.call_tool("a", {}))

    def test_errors_never_carry_the_credential_url_or_body(self):
        cases = {401: "refused the credential", 302: "redirects are never followed", 500: "HTTP 500"}
        for status, text in cases.items():
            def script(method, message, status=status):
                return status, {"Location": "https://evil.example/" + SECRET}, b"body with " + SECRET.encode()

            with self.assertRaises(http.UpstreamTransportError) as caught:
                with session_with(script, []):
                    pass
            self.assertIn(text, str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn("evil.example", str(caught.exception))

    def test_jsonrpc_errors_oversize_and_unbounded_listing_are_failures(self):
        def error_script(method, message):
            if message.get("method") == "initialize":
                return 200, {"Content-Type": "application/json"}, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": SECRET}}).encode()
            return 202, {}, b""

        with self.assertRaises(http.UpstreamTransportError) as caught:
            with session_with(error_script, []):
                pass
        self.assertNotIn(SECRET, str(caught.exception))

        def huge(method, message):
            return 200, {"Content-Type": "application/json"}, b"x" * (http.MAX_RESPONSE_BYTES + 5)

        with self.assertRaisesRegex(http.UpstreamTransportError, "too large"):
            with session_with(huge, []):
                pass

        def endless(method, message):
            if message.get("method") == "tools/list":
                return json_reply(message, {"tools": [], "nextCursor": "more"})
            return script_for([])(method, message)

        with session_with(endless, []) as session:
            with self.assertRaisesRegex(http.UpstreamTransportError, "unbounded"):
                session.list_tools()


def selfsigned(host):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
                   .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))


class PinnedTlsTests(unittest.TestCase):
    def test_connection_dials_the_pinned_ip_and_verifies_the_declared_name(self):
        cert, key = selfsigned("mcp.example.test")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cert_path, key_path = pathlib.Path(tmp.name) / "c.pem", pathlib.Path(tmp.name) / "k.pem"
        cert_path.write_bytes(cert)
        key_path.write_bytes(key)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]

        def serve():
            for _ in range(2):
                try:
                    raw, _ = listener.accept()
                    with server_context.wrap_socket(raw, server_side=True) as tls:
                        tls.recv(4096)
                        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                except (ssl.SSLError, OSError):
                    pass

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(listener.close)
        trust = ssl.create_default_context(cafile=str(cert_path))
        good = http.PinnedHTTPSConnection("mcp.example.test", "127.0.0.1", port, timeout=5, context=trust)
        good.request("GET", "/")
        self.assertEqual(good.getresponse().read(), b"ok")
        good.close()
        wrong = http.PinnedHTTPSConnection("other.example.test", "127.0.0.1", port, timeout=5, context=trust)
        with self.assertRaises(ssl.SSLError):
            wrong.request("GET", "/")
        wrong.close()


class SecretAndInstallTests(unittest.TestCase):
    def test_secret_resolution_is_by_reference_and_fails_closed(self):
        name = http.secret_env_name(SECRET_REF)
        self.assertTrue(name.startswith("MODEL_GARDEN_SECRET_") and SECRET_REF not in name)
        self.assertEqual(http.EnvSecretResolver({name: SECRET})(SECRET_REF), SECRET)
        with self.assertRaises(http.UpstreamTransportError):
            http.EnvSecretResolver({})(SECRET_REF)

    def test_nothing_is_installed_unless_the_deployment_enables_upstream_mcp(self):
        self.assertIsNone(http.install({}))
        self.assertIsNone(http.install({"MODEL_GARDEN_UPSTREAM_MCP": "yes"}))
        installed = http.install({"MODEL_GARDEN_UPSTREAM_MCP": "enabled"})
        self.assertEqual(set(installed), {"upstream_transport", "upstream_dns_resolver", "upstream_secret_resolver"})


class SidecarExposureTests(unittest.TestCase):
    READ_TOOL = {"name": "create_contact", "description": "Create an approved CRM record",
                 "inputSchema": {"type": "object", "properties": {"fields": {"type": "object"}}, "required": ["fields"], "additionalProperties": False}}
    DECLARATION = {"name": "hubspot", "url": "https://mcp.hubspot.example/api/mcp", "credentialRef": SECRET_REF, "owner": "owner@acme.example",
                   "tools": {"create_contact": {"capability": "crm.create", "risk": "write"}}}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        workspace = self.root / "workspace"
        shutil.copytree(ROOT / "examples/workspace", workspace)
        desired = compiler.compile_workspace(workspace, "receptionist")[0]
        desired["source"]["client"] = "acme"
        for tool in desired["tools"]:
            if tool["metadata"]["name"] == "crm.create":
                tool["spec"]["type"] = "mcp"
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired))
        manifest = {"client": "acme", "environment": "dev", "capabilities": [{"family": "crm", "provider": "hubspot", "status": "available", "tier": 2}]}
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        self.path, self.manifest = path, manifest

    def runtime(self, **extra):
        runtime = runtime_module.GovernedToolRuntime(
            self.path, self.root / "audit.jsonl", self.root / "runtime-approvals",
            environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")}, **extra,
        )
        runtime.capabilities = lambda: self.manifest
        runtime.governance_root.mkdir(exist_ok=True)
        return runtime

    def activate(self, runtime):
        registry = grants.GrantRegistry(runtime.governance_root / "provider-grants", tenant="acme-dev")
        registry.create(agent="receptionist", family="crm", provider="hubspot", connection_id="crm-main", isolation="own-identity",
                        acting_identity="bot@acme.example", secret_ref=SECRET_REF, scopes=["crm.create"], owner="owner@acme.example",
                        by="operator@acme.example", now=NOW)
        approvals = mcp.SchemaApprovals(runtime.governance_root / "mcp-schema-approvals", tenant="acme-dev")
        approvals.approve("hubspot", self.READ_TOOL, by="reviewer@platform.example", now=NOW)
        store = runtime_module.MCP_CONNECTIONS.McpConnectionStore(runtime.governance_root / "mcp-connections", tenant="acme-dev")
        store.stage("crm-main", agent="receptionist", declaration=self.DECLARATION, by="operator@acme.example", now=NOW)
        store.activate("crm-main", agent="receptionist", consented_by="owner@acme.example", by="operator@acme.example",
                       offered=[self.READ_TOOL], approvals=approvals, grants=registry, now=NOW + 1)
        return store

    def installed(self):
        return {"upstream_transport": lambda *a: None, "upstream_dns_resolver": lambda h: [], "upstream_secret_resolver": lambda r: SECRET}

    def test_nothing_is_offered_without_an_installed_transport_or_an_active_connection(self):
        plain = self.runtime()
        self.activate(plain)
        self.assertEqual(plain.upstream_tools(), [])
        wired = self.runtime(**self.installed())
        self.assertEqual([(t["tool"], t["risk"]) for t in wired.upstream_tools()], [("crm.create", "write")])
        offered = json.dumps(wired.upstream_tools())
        self.assertNotIn("hubspot.example", offered)
        self.assertNotIn(SECRET_REF, offered)

    def test_a_disabled_connection_is_withdrawn_and_an_unselected_tool_is_never_offered(self):
        runtime = self.runtime(**self.installed())
        store = self.activate(runtime)
        store.disable("crm-main", agent="receptionist", by="operator@acme.example")
        self.assertEqual(runtime.upstream_tools(), [])

    def test_server_registers_the_portable_tool_name_only_when_offered(self):
        plain = self.runtime()
        builtin = asyncio.run(runtime_module.build_server(plain).list_tools())
        self.assertIn("Create a new customer record", next(t.description for t in builtin if t.name == "crm_create"))
        wired = self.runtime(**self.installed())
        self.activate(wired)
        server = runtime_module.build_server(wired)
        listed = asyncio.run(server.list_tools())
        crm = [t for t in listed if t.name == "crm_create"]
        self.assertEqual(len(crm), 1)
        self.assertIn("approved `crm.create` operation", crm[0].description)
        self.assertFalse({t.name for t in listed if "upstream" in t.name or "http" in t.name or "fetch" in t.name})


if __name__ == "__main__":
    unittest.main()
