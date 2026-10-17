"""Credential-free checks that `docs.create` is served from the Tier 0 artifact store through the governed boundary (#77, #101)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compiler = _load("compile_workspace_for_docs_runtime", "scripts/compile-workspace.py")
runtime_module = _load("governed_mcp_for_docs_runtime", "platform/runtime/governed_mcp.py")
approval_module = _load("runtime_approval_for_docs", "scripts/runtime-approval.py")
channel = _load("text_channel_for_docs", "platform/channels/text_channel.py")
provisioner = _load("provision_hermes_for_docs", "scripts/provision-hermes-profile.py")
deployer = _load("deploy_client_docker_for_docs", "scripts/deploy-client-docker.py")

DOCS_BINDING = {"environment": "dev", "connectors": {"docs": {"provider": "built-in-artifacts", "tier": 0, "owner": "owner@client.example"}}}
PRIVATE = "Hargreaves boiler at 12 Acacia Road, card 4111 1111 1111 1111"
TOOL = """apiVersion: modelgarden.ai/v1
kind: Tool
metadata:
  name: docs.create
  owner: ai-platform
  description: Keep a finished document for the requester in the client's own store.
spec:
  type: internal
  capability: docs
  risk: write
  approvalRequired: true
"""
CHANNEL_BINDING = {
    "apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding",
    "spec": {"environment": "prod", "channels": {"directory": {"slack": {"T01/U-ALICE": {"identity": "alice@acme.example"}}}}},
}


def workspace(tmp: str) -> pathlib.Path:
    target = pathlib.Path(tmp) / "workspace"
    shutil.copytree(ROOT / "examples" / "workspace", target, dirs_exist_ok=True)
    (target / "tools" / "docs-create.yaml").write_text(TOOL, encoding="utf-8")
    agent = target / "agents" / "front-office" / "receptionist" / "agent.yaml"
    document = yaml.safe_load(agent.read_text())
    document["spec"]["tools"].append("docs.create")
    agent.write_text(yaml.safe_dump(document, sort_keys=False))
    return target


def principal_for(user: str):
    message = channel.InboundMessage(
        channel="slack", conversation_id="C1", thread_id="1", message_id="2",
        sender=channel.ChannelIdentity("slack", user, workspace_id="T01"), text="write a report",
    )
    return channel.PrincipalDirectory.from_binding(CHANNEL_BINDING).principal_for(message)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def runtime(self, binding=DOCS_BINDING, principal=None, client="acme"):
        desired = compiler.compile_workspace(workspace(str(self.root)), "receptionist")[0]
        desired["source"]["client"] = client
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        environ = {"MODEL_GARDEN_INITIATING_IDENTITY": "alice@acme.example"}
        if binding is not None:
            manifest = runtime_module.CAPABILITIES.build_manifest(desired, binding)
            (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(self.root / "manifest.json")
        return runtime_module.GovernedToolRuntime(
            path, self.root / "audit.jsonl", self.root / "approvals", environ=environ,
            principal_resolver=(lambda: principal) if principal is not None else None,
        )

    def store(self, tenant="acme"):
        return runtime_module.ARTIFACTS.ArtifactStore(self.root / "artifacts", tenant=tenant)

    def audit(self) -> list[dict]:
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]

    def approve(self, pending: dict) -> None:
        approval_module.decide(self.root / "approvals", pending["request"]["requestId"], approved=True, approver="owner@client.example")

    def create(self, runtime, title="Weekly report", content="# Report\n\nAll good.", **kwargs):
        pending = runtime.docs_create(title=title, content=content, **kwargs)
        self.assertEqual(pending["status"], "pending-approval")
        self.approve(pending)
        return runtime.docs_create(title=title, content=content, **kwargs)


class CreateTests(Case):
    def test_a_document_waits_for_exact_approval_then_lands_in_the_tier_zero_store(self):
        runtime = self.runtime()
        result = self.create(runtime, justification="the owner asked for the weekly report")
        self.assertEqual(result["status"], "executed")
        document = result["result"]
        self.assertTrue(document["documentId"].startswith("tier0:acme/a-"))
        self.assertFalse(document["duplicate"])
        (entry,) = self.store().list()
        self.assertEqual((entry["name"], entry["mediaType"], entry["requestedBy"], entry["agent"]), ("Weekly report.md", "text/markdown", "alice@acme.example", "receptionist"))
        _, content = self.store().get(entry["id"], requested_by="alice@acme.example")
        self.assertEqual(content.decode(), "# Report\n\nAll good.")

    def test_the_ledger_records_location_and_digest_at_tier_zero_never_the_text(self):
        runtime = self.runtime()
        self.create(runtime, title="Boiler follow-up", content=f"Notes: {PRIVATE}")
        execution = next(event for event in self.audit() if event["event"] == "execution")
        ledger = execution["ledger"]
        self.assertEqual((ledger["provider"], ledger["tier"], ledger["capability"]), ("built-in-artifacts", 0, "docs"))
        self.assertTrue(ledger["externalObjectRef"].startswith("tier0:acme/a-"))
        self.assertEqual(execution["arguments"]["content"], runtime_module.GOVERN.REDACTED)
        text = (self.root / "audit.jsonl").read_text()
        for private in ("Hargreaves", "Acacia", "4111", "Notes:"):
            self.assertNotIn(private, text)
        created = next(event for event in self.audit() if event.get("action") == "artifact.create")
        self.assertEqual(created["details"]["location"], ledger["externalObjectRef"])
        self.assertEqual(len(created["details"]["sha256"]), 64)
        self.assertTrue(runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_the_same_document_from_the_same_person_is_not_stored_twice(self):
        runtime = self.runtime()
        first = self.create(runtime)["result"]
        again = self.create(runtime)["result"]
        self.assertEqual((again["documentId"], again["duplicate"]), (first["documentId"], True))
        self.assertEqual(len(self.store().list()), 1)

    def test_bad_input_fails_closed_and_stores_nothing(self):
        runtime = self.runtime()
        for title, content in (("", "text"), ("T", ""), ("T", "x" * (runtime_module.ARTIFACTS.MAX_DOCUMENT_CHARS + 1))):
            with self.assertRaises(ValueError, msg=(title, len(content))):
                self.create(runtime, title=title, content=content)
        self.assertEqual(self.store().list(), [])

    def test_only_markdown_text_is_stored_and_a_hostile_title_cannot_walk_the_filesystem(self):
        runtime = self.runtime()
        self.create(runtime, title="../../etc/passwd", content="plain text")
        (entry,) = self.store().list()
        self.assertNotIn("/", entry["name"])
        self.assertTrue(entry["name"].endswith(".md"))
        self.assertTrue((self.root / "artifacts" / "acme" / entry["id"]).is_file())
        self.assertFalse((self.root / "etc").exists())


class BindingTests(Case):
    def test_unbound_or_wrongly_bound_docs_is_refused_with_the_hand_off_wording(self):
        for binding in (None, {"environment": "dev", "connectors": {}},
                        {"environment": "dev", "connectors": {"docs": {"provider": "google-docs", "tier": 2}}}):
            runtime = self.runtime(binding=binding)
            with self.assertRaisesRegex(PermissionError, "document store is not connected"):
                runtime.docs_create(title="T", content="text")
        self.assertEqual(self.store().list(), [])

    def test_the_store_is_per_client(self):
        self.create(self.runtime(client="acme"))
        self.assertEqual(len(self.store("acme").list()), 1)
        self.assertEqual(self.store("other").list(), [])

    def test_a_client_without_a_compiled_slug_is_refused(self):
        runtime = self.runtime(client="Not A Slug!")
        with self.assertRaisesRegex(PermissionError, "tenant-scoped"):
            runtime.docs_create(title="T", content="text")

    def test_a_public_caller_cannot_create_documents(self):
        runtime = self.runtime(principal=principal_for("U-STRANGER"))
        with self.assertRaises(PermissionError):
            runtime.docs_create(title="T", content="text")
        self.assertEqual(self.store().list(), [])


class SurfaceTests(Case):
    def test_the_mcp_server_exposes_docs_create_and_hermes_maps_the_tool(self):
        runtime = self.runtime()
        names = {tool.name for tool in asyncio.run(runtime_module.build_server(runtime).list_tools())}
        self.assertIn("docs_create", names)
        self.assertEqual(provisioner.MCP_TOOL_NAMES["docs.create"], "docs_create")
        self.assertIn("docs.create", deployer.BUILT_IN_TOOLS)

    def test_the_registry_lists_the_built_in_provider_as_tier_zero_credential_free(self):
        registry = _load("registry_for_docs", "platform/core/integrations.py")
        provider = next(item for item in registry.load_registry()["providers"] if item["id"] == "built-in-artifacts")
        self.assertEqual((provider["family"], provider["tier"], provider["status"], provider["tools"]), ("docs", 0, "built-credential-free", ["docs.create", "docs.read"]))
        self.assertNotIn("proof", provider)

    def test_a_skill_needing_docs_is_ready_with_the_built_in_provider_only(self):
        desired = compiler.compile_workspace(workspace(str(self.root)), "receptionist")[0]
        manifest = runtime_module.CAPABILITIES.build_manifest(desired, DOCS_BINDING)
        entry = next(item for item in manifest["capabilities"] if item["family"] == "docs")
        self.assertEqual((entry["status"], entry["provider"], entry["tier"]), ("available", "built-in-artifacts", 0))
        forged = {"environment": "dev", "connectors": {"docs": {"provider": "built-in-artifacts", "tier": 1}}}
        entry = next(item for item in runtime_module.CAPABILITIES.build_manifest(desired, forged)["capabilities"] if item["family"] == "docs")
        self.assertNotEqual(entry["status"], "available")


if __name__ == "__main__":
    unittest.main()
