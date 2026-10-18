"""Credential-free checks that `files.*` and `docs.read` are served from the Tier 0 store through the governed boundary (#77, #91)."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_docs_runtime import (  # noqa: E402 - shared fixtures
    ROOT, approval_module, compiler, runtime_module,
)

BINDING = {"environment": "dev", "connectors": {
    "files": {"provider": "built-in-files", "tier": 0, "owner": "owner@client.example"},
    "docs": {"provider": "built-in-artifacts", "tier": 0, "owner": "owner@client.example"},
}}
TOOLS = {
    "files.search": ("files", "read", False), "files.read": ("files", "read", False),
    "files.write": ("files", "write", True), "docs.read": ("docs", "read", False),
}
PRIVATE = "Hargreaves boiler at 12 Acacia Road, card 4111 1111 1111 1111"


def workspace(tmp: str) -> pathlib.Path:
    target = pathlib.Path(tmp) / "workspace"
    shutil.copytree(ROOT / "examples" / "workspace", target, dirs_exist_ok=True)
    agent = target / "agents" / "front-office" / "receptionist" / "agent.yaml"
    document = yaml.safe_load(agent.read_text())
    for name, (capability, risk, approval) in TOOLS.items():
        (target / "tools" / (name.replace(".", "-") + ".yaml")).write_text(yaml.safe_dump({
            "apiVersion": "modelgarden.ai/v1", "kind": "Tool",
            "metadata": {"name": name, "owner": "ai-platform", "description": f"Tier 0 {name}."},
            "spec": {"type": "internal", "capability": capability, "risk": risk, "approvalRequired": approval},
        }, sort_keys=False))
        document["spec"]["tools"].append(name)
    agent.write_text(yaml.safe_dump(document, sort_keys=False))
    return target


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def runtime(self, binding=BINDING, identity="alice@acme.example", client="acme"):
        desired = compiler.compile_workspace(workspace(str(self.root)), "receptionist")[0]
        desired["source"]["client"] = client
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        manifest = runtime_module.CAPABILITIES.build_manifest(desired, binding)
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        environ = {"MODEL_GARDEN_INITIATING_IDENTITY": identity, "MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")}
        return runtime_module.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals", environ=environ)

    def audit(self) -> list[dict]:
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]

    def write(self, runtime, name="notes", content="# Notes\n\nCall back Monday."):
        pending = runtime.files_write(name=name, content=content)
        self.assertEqual(pending["status"], "pending-approval")
        approval_module.decide(self.root / "approvals", pending["request"]["requestId"], approved=True, approver="owner@client.example")
        return runtime.files_write(name=name, content=content)


class FilesTests(Case):
    def test_write_waits_for_approval_then_search_and_read_return_the_requesters_file(self):
        runtime = self.runtime()
        written = self.write(runtime)["result"]
        self.assertTrue(written["externalObjectRef"].startswith("tier0:acme/a-"))
        found = runtime.files_search(query="note")["result"]["files"]
        self.assertEqual([item["name"] for item in found], ["notes.md"])
        read = runtime.files_read(file_id=found[0]["fileId"])["result"]["file"]
        self.assertEqual(read["text"], "# Notes\n\nCall back Monday.")
        document = runtime.docs_read(document_id=found[0]["fileId"])["result"]["document"]
        self.assertEqual(document["fileId"], found[0]["fileId"])

    def test_another_person_never_sees_the_file(self):
        self.write(self.runtime())
        other = self.runtime(identity="bob@acme.example")
        self.assertEqual(other.files_search(query="note")["result"]["files"], [])
        found = self.runtime().files_search(query="note")["result"]["files"]
        with self.assertRaises(Exception):
            other.files_read(file_id=found[0]["fileId"])

    def test_the_ledger_keeps_location_and_never_the_content(self):
        runtime = self.runtime()
        self.write(runtime, name="boiler", content=f"Notes: {PRIVATE}")
        runtime.files_search(query="boiler")
        text = re.sub(r"[0-9a-f]{16,}|\d{2}:\d{2}:\d{2}\.\d+Z", "", (self.root / "audit.jsonl").read_text())
        for private in ("Hargreaves", "Acacia", "4111", "Notes:"):
            self.assertNotIn(private, text)
        execution = next(event for event in self.audit() if event["event"] == "execution" and event["tool"] == "files.write")
        self.assertEqual((execution["ledger"]["provider"], execution["ledger"]["tier"], execution["ledger"]["capability"]), ("built-in-files", 0, "files"))
        self.assertTrue(runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_unbound_files_use_the_hand_off_wording_and_a_folder_is_not_a_model_argument(self):
        unbound = self.runtime(binding={"environment": "dev", "connectors": {}})
        with self.assertRaises(PermissionError) as raised:
            unbound.files_search(query="note")
        self.assertIn("human hand-off", str(raised.exception))
        self.assertNotIn("folder_id", runtime_module.GovernedToolRuntime.files_write.__code__.co_varnames)

    def test_the_mcp_server_exposes_the_file_tools(self):
        import asyncio
        runtime = self.runtime()
        server = runtime_module.build_server(runtime)
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertTrue({"files_search", "files_read", "files_write", "docs_read"} <= names)


if __name__ == "__main__":
    unittest.main()
