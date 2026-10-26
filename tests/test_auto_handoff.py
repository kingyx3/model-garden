"""Unbound portable write Tools should have a normal governed human path, not require a separate model-invented Tool call."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


compiler = load("compiler_auto_handoff", "scripts/compile-workspace.py")
runtime_module = load("runtime_auto_handoff", "platform/runtime/governed_mcp.py")
ARGS = {"start": "2026-10-27T10:00:00+08:00", "end": "2026-10-27T10:30:00+08:00", "summary": "Synthetic booking"}


class AutomaticUnboundHandoffTests(unittest.TestCase):
    def runtime(self, root, *, available=False, task_resolver=None):
        desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
        desired["source"]["client"] = "fixture-tenant"
        (root / "desired.json").write_text(json.dumps(desired), encoding="utf-8")
        manifest = runtime_module.CAPABILITIES.build_manifest(desired, {"connectors": {}})
        if available:
            calendar = next(c for c in manifest["capabilities"] if c["family"] == "calendar")
            calendar.update(status="available", provider="approved-upstream", tier=2)
            calendar.pop("fallback", None)
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return runtime_module.GovernedToolRuntime(
            root / "desired.json", root / "audit.jsonl", root / "approvals",
            environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(root / "manifest.json"),
                     "MODEL_GARDEN_HANDOFF_ASSIGNEE": "desk@fixture.example"},
            task_resolver=task_resolver,
        )

    def test_unbound_write_is_discoverable_and_invocation_never_executes_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root)
            server = runtime_module.build_server(runtime)
            names = {tool.name for tool in asyncio.run(server.list_tools())}
            self.assertIn("calendar_book", names)
            self.assertNotIn("calendar_availability", names)
            asyncio.run(server.call_tool("calendar_book", {"arguments": ARGS}))
            pending = runtime_module.HANDOFF.pending(root / "handoffs")
            self.assertEqual(len(pending), 1)
            self.assertEqual((pending[0]["tool"], pending[0]["assignee"], pending[0]["status"]),
                             ("calendar.book", "desk@fixture.example", "pending"))
            self.assertIn("NOT been done", runtime.handoff_status(pending[0]["id"])["message"])
            events = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
            self.assertEqual([event["outcome"] for event in events if event["event"] == "handoff"], ["created"])
            self.assertTrue(runtime_module.GOVERN.verify_audit_chain(root / "audit.jsonl")["valid"])

    def test_available_provider_does_not_offer_fake_handoff_or_allow_manual_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root, available=True)
            names = {tool.name for tool in asyncio.run(runtime_module.build_server(runtime).list_tools())}
            self.assertNotIn("calendar_book", names)
            with self.assertRaisesRegex(ValueError, "available here"):
                runtime.request_handoff(tool="calendar.book", arguments=ARGS)
            self.assertEqual(runtime_module.HANDOFF.pending(root / "handoffs"), [])

    def test_unbound_handoff_obeys_owner_pause_and_delegated_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root)
            runtime_module.PAUSE.PauseStore(root).pause("owner@fixture.example", "incident", 100)
            with self.assertRaisesRegex(PermissionError, "paused"):
                runtime.request_handoff(tool="calendar.book", arguments=ARGS)
            self.assertEqual(runtime_module.HANDOFF.pending(root / "handoffs"), [])
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root, task_resolver=lambda: {
                "governance": {"taskId": "synthetic-task"}, "authority": [], "halt": None, "charge": None,
            })
            with self.assertRaisesRegex(PermissionError, "delegated task"):
                runtime.request_handoff(tool="calendar.book", arguments=ARGS)
            self.assertEqual(runtime_module.HANDOFF.pending(root / "handoffs"), [])


if __name__ == "__main__":
    unittest.main()
