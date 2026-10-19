"""Credential-free checks that a client's own REST API runs through the governed boundary, to its pinned host (#159, #89)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_docs_runtime import ROOT, approval_module, compiler, runtime_module  # noqa: E402 - shared fixtures

api = runtime_module.CUSTOM_API
http_module = runtime_module.CUSTOM_API_HTTP
BINDING = {"environment": "dev", "connectors": {}}
REVIEWER = "reviewer@platform.example"
PRIVATE = "Hargreaves boiler, card 4111 1111 1111 1111"
DEFINITION = {
    "name": "shop", "baseUrl": "https://api.shop.example/v2", "owner": "owner@client.example",
    "auth": {"type": "bearer", "credentialRef": "secret://shop/acme-dev"},
    "operations": {
        "get-order": {"method": "GET", "path": "/orders/{orderId}", "summary": "Fetch one order",
                      "parameters": {"orderId": {"in": "path", "required": True, "schema": {"type": "string", "maxLength": 40}}}},
        "add-note": {"method": "POST", "path": "/orders/{orderId}/notes", "summary": "Add a note to an order",
                     "parameters": {"orderId": {"in": "path", "required": True, "schema": {"type": "integer"}}},
                     "body": {"type": "object", "properties": {"text": {"type": "string", "maxLength": 500}}, "required": ["text"]}},
    },
}
TOOLS = {"api.shop.get-order": ("read", False), "api.shop.add-note": ("write", True)}


def workspace(tmp: str, tools=TOOLS) -> pathlib.Path:
    target = pathlib.Path(tmp) / "workspace"
    shutil.copytree(ROOT / "examples" / "workspace", target, dirs_exist_ok=True)
    agent = target / "agents" / "front-office" / "receptionist" / "agent.yaml"
    document = yaml.safe_load(agent.read_text())
    for name, (risk, approval) in tools.items():
        (target / "tools" / (name.replace(".", "-") + ".yaml")).write_text(yaml.safe_dump({
            "apiVersion": "modelgarden.ai/v1", "kind": "Tool",
            "metadata": {"name": name, "owner": "ai-platform", "description": f"Customer API operation {name}."},
            "spec": {"type": "http", "risk": risk, "approvalRequired": approval},
        }, sort_keys=False))
        document["spec"]["tools"].append(name)
    agent.write_text(yaml.safe_dump(document, sort_keys=False))
    return target


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.sent: list[dict] = []

    def send(self, request, *, auth, credential, pinned_ip):
        self.sent.append({"request": request, "credential": credential, "ip": pinned_ip})
        return {"status": 200, "body": {"id": 7, "state": "open"}}

    def runtime(self, definitions=(DEFINITION,), tools=TOOLS, resolver=lambda host: ["93.184.216.34"], send=None, secret=lambda ref: "s3cr3t-token"):
        desired = compiler.compile_workspace(workspace(str(self.root), tools), "receptionist")[0]
        desired["source"]["client"] = "acme"
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        manifest = runtime_module.CAPABILITIES.build_manifest(desired, BINDING)
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        environ = {"MODEL_GARDEN_INITIATING_IDENTITY": "alice@acme.example", "MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")}
        custom = {"definitions": list(definitions), "send": send or self.send, "dns_resolver": resolver, "secret_resolver": secret}
        return runtime_module.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals", environ=environ, custom_api=custom)

    def approvals(self):
        return runtime_module.MCP_EXECUTOR._mcp().SchemaApprovals(self.root / "api-approvals", tenant="acme-dev")

    def approve_operations(self, definition=DEFINITION, only=None):
        for tool in api.operation_tools(definition):
            if only is None or tool["name"] in only:
                self.approvals().approve(definition["name"], tool, by=REVIEWER, now=1_800_000_000)

    def audit(self):
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]

    def decide(self, pending):
        approval_module.decide(self.root / "approvals", pending["request"]["requestId"], approved=True, approver="owner@client.example")


class CallTests(Case):
    def test_nothing_is_offered_until_an_owner_approves_the_exact_operation(self):
        runtime = self.runtime()
        self.assertEqual(runtime.custom_api_tools(), [])
        self.approve_operations(only={"get-order"})
        self.assertEqual([item["tool"] for item in runtime.custom_api_tools()], ["api.shop.get-order"])
        with self.assertRaises(Exception):
            runtime.custom_api_call("shop", "add-note", {"orderId": 1, "body": {"text": "x"}})  # unapproved: refused before anything is sent
        self.assertEqual(self.sent, [])

    def test_a_read_runs_to_the_pinned_host_with_the_credential_and_is_ledgered(self):
        self.approve_operations()
        runtime = self.runtime()
        result = runtime.custom_api_call("shop", "get-order", {"orderId": "A-17"}, justification="the caller asked about their order")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["result"]["body"], {"id": 7, "state": "open"})
        (call,) = self.sent
        self.assertEqual((call["request"]["method"], call["request"]["url"], call["ip"], call["credential"]),
                         ("GET", "https://api.shop.example/v2/orders/A-17", "93.184.216.34", "s3cr3t-token"))
        execution = next(event for event in self.audit() if event["event"] == "execution")
        self.assertEqual((execution["ledger"]["provider"], execution["ledger"]["tier"]), ("custom-api:shop", 3))
        text = (self.root / "audit.jsonl").read_text()
        self.assertNotIn("s3cr3t-token", text)
        self.assertTrue(runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_a_write_waits_for_exact_approval_and_the_body_stays_out_of_the_ledger(self):
        self.approve_operations()
        runtime = self.runtime()
        arguments = {"orderId": 7, "body": {"text": f"Notes: {PRIVATE}"}}
        pending = runtime.custom_api_call("shop", "add-note", arguments)
        self.assertEqual(pending["status"], "pending-approval")
        self.assertEqual(self.sent, [])
        self.decide(pending)
        done = runtime.custom_api_call("shop", "add-note", arguments)
        self.assertEqual(done["status"], "executed")
        self.assertEqual(self.sent[0]["request"]["body"], {"text": f"Notes: {PRIVATE}"})
        text = (self.root / "audit.jsonl").read_text()
        for private in ("Hargreaves", "4111", "Notes:"):
            self.assertNotIn(private, text)

    def test_changing_an_operation_after_approval_hides_it_until_it_is_reviewed_again(self):
        self.approve_operations()
        changed = json.loads(json.dumps(DEFINITION))
        changed["operations"]["get-order"]["path"] = "/orders/{orderId}/full"
        runtime = self.runtime(definitions=(changed,))
        self.assertEqual([item["operation"] for item in runtime.custom_api_tools()], ["add-note"])
        with self.assertRaises(Exception):
            runtime.custom_api_call("shop", "get-order", {"orderId": "A-17"})
        self.assertEqual(self.sent, [])

    def test_an_argument_cannot_leave_the_declared_path_or_host(self):
        self.approve_operations()
        runtime = self.runtime()
        for bad in ({"orderId": "../admin"}, {"orderId": "1/../../x"}, {"orderId": "a", "host": "evil.example"}, {}):
            with self.assertRaises(Exception):
                runtime.custom_api_call("shop", "get-order", bad)
        self.assertEqual(self.sent, [])

    def test_a_private_or_unresolvable_host_is_refused_and_nothing_is_sent(self):
        self.approve_operations()
        for resolver in (lambda host: ["10.0.0.5"], lambda host: ["169.254.169.254"], lambda host: []):
            runtime = self.runtime(resolver=resolver)
            with self.assertRaises(Exception):
                runtime.custom_api_call("shop", "get-order", {"orderId": "A-17"})
        self.assertEqual(self.sent, [])

    def test_a_missing_credential_or_transport_error_never_echoes_detail(self):
        self.approve_operations()

        def boom(*args, **kwargs):
            raise OSError("connect to https://api.shop.example/v2/orders/A-17 with Bearer s3cr3t-token failed")

        for kwargs in ({"secret": lambda ref: (_ for _ in ()).throw(KeyError(ref))}, {"send": boom}):
            runtime = self.runtime(**kwargs)
            with self.assertRaises(RuntimeError) as raised:
                runtime.custom_api_call("shop", "get-order", {"orderId": "A-17"})
            self.assertNotIn("s3cr3t-token", str(raised.exception))
            self.assertNotIn("A-17", str(raised.exception))

    def test_an_operation_without_a_compiled_risk_matched_http_tool_is_not_offered_or_run(self):
        self.approve_operations()
        runtime = self.runtime(tools={"api.shop.get-order": ("write", True)})  # risk differs from the declared read
        self.assertEqual(runtime.custom_api_tools(), [])
        with self.assertRaises(PermissionError):
            runtime.custom_api_call("shop", "get-order", {"orderId": "A-17"})
        with self.assertRaises(PermissionError):
            runtime.custom_api_call("shop", "add-note", {"orderId": 1, "body": {"text": "x"}})

    def test_the_mcp_server_exposes_only_approved_api_operations(self):
        self.approve_operations(only={"get-order"})
        server = runtime_module.build_server(self.runtime())
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertIn("api_shop_get_order", names)
        self.assertNotIn("api_shop_add_note", names)

    def test_without_the_sender_installed_nothing_is_offered(self):
        desired = compiler.compile_workspace(workspace(str(self.root)), "receptionist")[0]
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        runtime = runtime_module.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals", environ={})
        self.assertEqual(runtime.custom_api_tools(), [])
        with self.assertRaises(PermissionError):
            runtime.custom_api_call("shop", "get-order", {"orderId": "A-17"})


class FakeConnection:
    def __init__(self, status=200, body=b'{"ok": true}', content_type="application/json", record=None):
        self.status, self.body, self.content_type, self.record = status, body, content_type, record if record is not None else []

    def request(self, method, target, body=None, headers=None):
        self.record.append({"method": method, "target": target, "body": body, "headers": dict(headers or {})})

    def getresponse(self):
        outer = self

        class Response:
            status = outer.status

            def getheader(self, name):
                return outer.content_type if name == "Content-Type" else None

            def read(self, limit):
                return outer.body[:limit]

        return Response()

    def close(self):
        pass


def request(**extra):
    return {"method": "GET", "url": "https://api.shop.example/v2/orders/7?limit=5", "limits": dict(api.LIMITS), **extra}


class SenderTests(unittest.TestCase):
    def send(self, auth=None, credential="tok", connection=None, **extra):
        record: list[dict] = []
        connection = connection or FakeConnection(record=record)
        result = http_module.send(request(**extra), auth=auth or {"type": "bearer"}, credential=credential, pinned_ip="93.184.216.34",
                                  connect=lambda host, ip, port, timeout: connection)
        return result, connection.record

    def test_each_authentication_style_puts_the_credential_in_one_place(self):
        _, (sent,) = self.send({"type": "bearer"})
        self.assertEqual((sent["target"], sent["headers"]["Authorization"]), ("/v2/orders/7?limit=5", "Bearer tok"))
        _, (sent,) = self.send({"type": "header", "header": "X-Api-Key"})
        self.assertEqual(sent["headers"]["X-Api-Key"], "tok")
        self.assertNotIn("Authorization", sent["headers"])
        _, (sent,) = self.send({"type": "basic"}, credential="user:pass")
        self.assertEqual(sent["headers"]["Authorization"], "Basic dXNlcjpwYXNz")
        _, (sent,) = self.send({"type": "none"}, credential=None)
        self.assertNotIn("Authorization", sent["headers"])

    def test_a_body_is_sent_as_canonical_json(self):
        _, (sent,) = self.send(method="POST", body={"b": 1, "a": "x"})
        self.assertEqual((sent["body"], sent["headers"]["Content-Type"]), (b'{"a":"x","b":1}', "application/json"))

    def test_results_are_bounded_and_text_is_marked_untrusted(self):
        result, _ = self.send(connection=FakeConnection(body=b"hello", content_type="text/plain"))
        self.assertEqual(result, {"status": 200, "text": "hello", "untrustedText": True})
        result, _ = self.send(connection=FakeConnection(body=b"", content_type=""))
        self.assertEqual(result, {"status": 200})

    def test_errors_carry_a_status_never_a_body_and_redirects_are_not_followed(self):
        for status, expect in ((302, "redirect"), (401, "(401)"), (404, "HTTP 404"), (500, "HTTP 500")):
            with self.assertRaises(http_module.CustomApiTransportError) as raised:
                self.send(connection=FakeConnection(status=status, body=b"customer secret"))
            self.assertIn(expect, str(raised.exception))
            self.assertNotIn("secret", str(raised.exception))
        with self.assertRaisesRegex(http_module.CustomApiTransportError, "too large"):
            self.send(connection=FakeConnection(body=b"x" * (api.LIMITS["maxResponseBytes"] + 5)))
        with self.assertRaisesRegex(http_module.CustomApiTransportError, "invalid JSON"):
            self.send(connection=FakeConnection(body=b"{nope"))

    def test_a_missing_credential_or_non_https_url_is_refused_before_connecting(self):
        with self.assertRaisesRegex(http_module.CustomApiTransportError, "credential"):
            self.send(credential=None)
        with self.assertRaises(http_module.CustomApiTransportError):
            http_module.send({**request(), "url": "http://api.shop.example/x"}, auth={"type": "none"}, credential=None, pinned_ip="93.184.216.34",
                             connect=lambda *a: self.fail("connected"))

    def test_install_is_off_unless_the_deployment_enables_it(self):
        self.assertIsNone(http_module.install({}))
        path = pathlib.Path(tempfile.mkdtemp()) / "apis.json"
        path.write_text(json.dumps([DEFINITION]))
        settings = http_module.install({"MODEL_GARDEN_CUSTOM_API": "enabled", "MODEL_GARDEN_CUSTOM_API_DEFINITIONS": str(path)})["custom_api"]
        self.assertEqual(settings["definitions"][0]["name"], "shop")
        self.assertIs(settings["send"], http_module.send)


class OperatorCommandTests(Case):
    def run_command(self, *args):
        import subprocess
        path = self.root / "apis.json"
        path.write_text(json.dumps([DEFINITION]))
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "runtime-custom-api.py"), str(self.root), "--tenant", "acme-dev",
                               "--definitions", str(path), *args], capture_output=True, text=True)

    def test_status_then_approve_makes_the_operation_offered_and_ledgers_the_reviewer(self):
        before = json.loads(self.run_command("status").stdout)["operations"]
        self.assertEqual({row["state"] for row in before}, {"unapproved"})
        done = self.run_command("approve", "--api", "shop", "--operation", "get-order", "--actor", REVIEWER)
        self.assertEqual(done.returncode, 0, done.stderr)
        states = {row["operation"]: row["state"] for row in json.loads(self.run_command("status").stdout)["operations"]}
        self.assertEqual(states, {"get-order": "approved", "add-note": "unapproved"})
        self.assertEqual([item["tool"] for item in self.runtime().custom_api_tools()], ["api.shop.get-order"])
        event = next(event for event in self.audit() if event.get("action") == "mcp.schema-approve")
        self.assertEqual(event["performedBy"], REVIEWER)

    def test_an_undeclared_operation_or_a_bad_reviewer_is_refused(self):
        self.assertEqual(self.run_command("approve", "--api", "shop", "--operation", "nope", "--actor", REVIEWER).returncode, 1)
        self.assertEqual(self.run_command("approve", "--api", "shop", "--operation", "get-order", "--actor", "not a person!").returncode, 1)


if __name__ == "__main__":
    unittest.main()
