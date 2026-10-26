"""The governed sidecar serves Model Garden's own capabilities, never bespoke SaaS clients."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


compiler = load("compile_workspace_for_mcp", "scripts/compile-workspace.py")
runtime_module = load("governed_mcp_runtime", "platform/runtime/governed_mcp.py")


class GovernedMcpRuntimeTests(unittest.TestCase):
    def test_retired_vendor_tools_are_not_exposed_and_tier_zero_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
            (root / "desired.json").write_text(json.dumps(desired), encoding="utf-8")
            runtime = runtime_module.GovernedToolRuntime(
                root / "desired.json", root / "audit.jsonl", root / "approvals", environ={},
            )
            names = {tool.name for tool in asyncio.run(runtime_module.build_server(runtime).list_tools())}
            self.assertTrue({"kb_search", "crm_create", "crm_read", "crm_log_interaction",
                             "platform_capabilities", "handoff_request", "handoff_status"}.issubset(names))
            self.assertIn("calendar_book", names)  # selected write: normal Tool prepares Tier 4 handoff
            self.assertNotIn("calendar_availability", names)  # unbound reads cannot be handed off
            self.assertFalse(hasattr(runtime, "calendar_book"))
            self.assertFalse(hasattr(runtime, "calendar_availability"))
            self.assertFalse((root / "audit.jsonl").exists())  # discovery never authorizes or contacts a provider


if __name__ == "__main__":
    unittest.main()
