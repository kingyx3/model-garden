"""Credential-free checks that `tickets.*` is served from the Tier 0 store through the governed boundary (#77)."""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_docs_runtime import (  # noqa: E402 - shared fixtures
    ROOT, approval_module, compiler, principal_for, runtime_module,
)

BINDING = {"environment": "dev", "connectors": {"tickets": {"provider": "built-in-tickets", "tier": 0, "owner": "owner@client.example"}}}
TOOLS = {"tickets.search": ("read", False), "tickets.create": ("write", True), "tickets.update": ("write", True)}
PRIVATE = "Hargreaves boiler at 12 Acacia Road, card 4111 1111 1111 1111"


def workspace(tmp: str) -> pathlib.Path:
    target = pathlib.Path(tmp) / "workspace"
    shutil.copytree(ROOT / "examples" / "workspace", target, dirs_exist_ok=True)
    agent = target / "agents" / "front-office" / "receptionist" / "agent.yaml"
    document = yaml.safe_load(agent.read_text())
    for name, (risk, approval) in TOOLS.items():
        (target / "tools" / (name.replace(".", "-") + ".yaml")).write_text(yaml.safe_dump({
            "apiVersion": "modelgarden.ai/v1", "kind": "Tool",
            "metadata": {"name": name, "owner": "ai-platform", "description": f"Tier 0 {name}."},
            "spec": {"type": "internal", "capability": "tickets", "risk": risk, "approvalRequired": approval},
        }, sort_keys=False))
        document["spec"]["tools"].append(name)
    agent.write_text(yaml.safe_dump(document, sort_keys=False))
    return target


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def runtime(self, user="U-ALICE", binding=BINDING, client="acme"):
        desired = compiler.compile_workspace(workspace(str(self.root)), "receptionist")[0]
        desired["source"]["client"] = client
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        environ = {}
        if binding is not None:
            manifest = runtime_module.CAPABILITIES.build_manifest(desired, binding)
            (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(self.root / "manifest.json")
        principal = principal_for(user)
        return runtime_module.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals", environ=environ,
                                                  principal_resolver=lambda: principal)

    def audit(self) -> list[dict]:
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]

    def approved(self, runtime, method, **arguments):
        pending = getattr(runtime, method)(**arguments)
        self.assertEqual(pending["status"], "pending-approval")
        approval_module.decide(self.root / "approvals", pending["request"]["requestId"], approved=True, approver="owner@client.example")
        return getattr(runtime, method)(**arguments)


class ServedTests(Case):
    def test_staff_open_search_and_update_a_ticket_through_approval(self):
        runtime = self.runtime()
        created = self.approved(runtime, "tickets_create", project="support", title="Boiler follow-up", description="Call Alex back")
        self.assertEqual(created["status"], "executed")
        ticket_id = created["result"]["ticketId"]
        self.assertEqual(created["result"]["externalObjectRef"], f"tier0:tickets/acme/{ticket_id}")
        found = runtime.tickets_search(query="boiler")
        self.assertEqual([item["ticketId"] for item in found["result"]["tickets"]], [ticket_id])
        self.assertEqual(found["result"]["tickets"][0]["description"], "Call Alex back")  # staff see the text
        updated = self.approved(runtime, "tickets_update", ticket_id=ticket_id, status="done", comment="Called, resolved")
        self.assertEqual(updated["result"]["status"], "done")
        with self.assertRaises(ValueError):  # done is final
            self.approved(runtime, "tickets_update", ticket_id=ticket_id, status="open")

    def test_the_ledger_names_project_id_and_status_never_the_text(self):
        runtime = self.runtime()
        created = self.approved(runtime, "tickets_create", project="support", title=PRIVATE, description=PRIVATE)
        self.approved(runtime, "tickets_update", ticket_id=created["result"]["ticketId"], status="waiting", comment=PRIVATE)
        runtime.tickets_search(query="Hargreaves")
        text = (self.root / "audit.jsonl").read_text()
        for private in ("Hargreaves", "Acacia", "4111"):
            self.assertNotIn(private, text)
        executions = {event["tool"]: event for event in self.audit() if event["event"] == "execution"}
        self.assertEqual(executions["tickets.create"]["arguments"]["project"], "support")
        self.assertEqual(executions["tickets.create"]["arguments"]["title"], runtime_module.GOVERN.REDACTED)
        self.assertEqual(executions["tickets.update"]["arguments"]["status"], "waiting")
        self.assertEqual(executions["tickets.create"]["ledger"]["provider"], "built-in-tickets")
        self.assertEqual(executions["tickets.create"]["ledger"]["tier"], 0)
        self.assertTrue(executions["tickets.create"]["ledger"]["externalObjectRef"].startswith("tier0:tickets/acme/t-"))
        reads = [event for event in self.audit() if event["event"] == "read" and event["tool"] == "tickets.search"]
        self.assertEqual(len(reads), 1)
        self.assertTrue(runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_an_unmapped_sender_cannot_open_or_change_tickets_and_sees_only_their_own(self):
        staff = self.runtime()
        self.approved(staff, "tickets_create", project="support", title="Team matter")
        stranger = self.runtime(user="U-STRANGER")
        self.assertEqual(stranger.tickets_search(query="*")["result"]["tickets"], [])  # no read of the team's list
        with self.assertRaises(PermissionError):  # a public principal never reaches a write without an explicit signed rule
            stranger.tickets_create(project="support", title="Please call me")
        with self.assertRaises(PermissionError):
            stranger.tickets_update(ticket_id="t-000000000000", status="done")

    def test_it_is_only_served_when_the_manifest_binds_tickets_to_the_built_in_store(self):
        for binding in (None, {"environment": "dev", "connectors": {}}, {"environment": "dev", "connectors": {"tickets": {"provider": "jira", "tier": 1}}}):
            runtime = self.runtime(binding=binding)
            with self.assertRaises(PermissionError, msg=str(binding)):
                runtime.tickets_search(query="*")
            with self.assertRaises(PermissionError):
                runtime.tickets_create(project="support", title="x")

    def test_tickets_are_kept_per_client(self):
        acme = self.runtime(client="acme")
        self.approved(acme, "tickets_create", project="support", title="Acme matter")
        other = self.runtime(client="globex")
        self.assertEqual(other.tickets_search(query="*")["result"]["tickets"], [])
        self.assertTrue((self.root / "tickets" / "acme.tickets.json").is_file())

    def test_the_sidecar_offers_the_three_tools_and_hermes_maps_them(self):
        provisioner = sys.modules["provision_hermes_for_docs"]
        for tool, name in (("tickets.search", "tickets_search"), ("tickets.create", "tickets_create"), ("tickets.update", "tickets_update")):
            self.assertEqual(provisioner.MCP_TOOL_NAMES[tool], name)
            self.assertIn(tool, sys.modules["deploy_client_docker_for_docs"].BUILT_IN_TOOLS)
        server = runtime_module.build_server(self.runtime())
        import asyncio
        offered = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertTrue({"tickets_search", "tickets_create", "tickets_update"} <= offered)


if __name__ == "__main__":
    unittest.main()
