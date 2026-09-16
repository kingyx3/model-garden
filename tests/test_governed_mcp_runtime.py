from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPILER_PATH = ROOT / "scripts" / "compile-workspace.py"
RUNTIME_PATH = ROOT / "platform" / "runtime" / "governed_mcp.py"
APPROVAL_PATH = ROOT / "scripts" / "runtime-approval.py"

compiler_spec = importlib.util.spec_from_file_location("compile_workspace_for_mcp", COMPILER_PATH)
compiler = importlib.util.module_from_spec(compiler_spec)
assert compiler_spec.loader is not None
compiler_spec.loader.exec_module(compiler)

runtime_spec = importlib.util.spec_from_file_location("governed_mcp_runtime", RUNTIME_PATH)
runtime_module = importlib.util.module_from_spec(runtime_spec)
assert runtime_spec.loader is not None
runtime_spec.loader.exec_module(runtime_module)

approval_spec = importlib.util.spec_from_file_location("runtime_approval", APPROVAL_PATH)
approval_module = importlib.util.module_from_spec(approval_spec)
assert approval_spec.loader is not None
approval_spec.loader.exec_module(approval_module)


class FakeCalendarTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers.copy(), payload))
        if url.endswith("/freeBusy"):
            return 200, {"calendars": {"primary": {"busy": []}}}
        return 200, {
            "id": "evt-123",
            "status": "confirmed",
            "htmlLink": "https://calendar.invalid/evt-123",
        }


class GovernedMcpRuntimeTests(unittest.TestCase):
    def _runtime(self, root: pathlib.Path, transport: FakeCalendarTransport):
        desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
        desired_path = root / "desired.json"
        desired_path.write_text(json.dumps(desired), encoding="utf-8")
        return runtime_module.GovernedToolRuntime(
            desired_path,
            root / "audit.jsonl",
            root / "approvals",
            environ={
                "MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN": "calendar-secret",
                "MODEL_GARDEN_INITIATING_IDENTITY": "call:trace-123",
            },
            calendar_transport=transport,
        )

    def test_mcp2_server_registers_only_the_runtime_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(pathlib.Path(tmp), FakeCalendarTransport())
            server = runtime_module.build_server(runtime)
            tools = asyncio.run(server.list_tools())
            self.assertEqual(sorted(tool.name for tool in tools), ["calendar_availability", "calendar_book"])

    def test_calendar_read_executes_without_approval_and_keeps_token_out_of_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            transport = FakeCalendarTransport()
            runtime = self._runtime(root, transport)

            result = runtime.calendar_availability(
                start="2026-09-16T10:00:00+08:00",
                end="2026-09-16T10:30:00+08:00",
                timezone="Asia/Singapore",
            )

            self.assertEqual(result["status"], "executed")
            self.assertTrue(result["result"]["available"])
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer calendar-secret")
            audit = (root / "audit.jsonl").read_text(encoding="utf-8")
            self.assertIn('"tool":"calendar.availability"', audit)
            self.assertIn('"outcome":"succeeded"', audit)
            self.assertNotIn("calendar-secret", audit)

    def test_calendar_write_waits_for_exact_single_use_approval_then_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            transport = FakeCalendarTransport()
            runtime = self._runtime(root, transport)
            arguments = {
                "start": "2026-09-16T10:00:00+08:00",
                "end": "2026-09-16T10:30:00+08:00",
                "summary": "Service appointment",
            }

            pending = runtime.calendar_book(**arguments)
            self.assertEqual(pending["status"], "pending-approval")
            self.assertEqual(transport.calls, [])
            request_id = pending["request"]["requestId"]
            pending_path = root / "approvals" / "pending" / f"{request_id}.json"
            decision_path = root / "approvals" / "decisions" / f"{request_id}.json"
            self.assertTrue(pending_path.is_file())
            self.assertEqual(
                approval_module.pending(root / "approvals")[0]["arguments"],
                arguments,
            )

            approval_module.decide(
                root / "approvals",
                request_id,
                approved=True,
                approver="owner@example.invalid",
            )
            executed = runtime.calendar_book(**arguments)

            self.assertEqual(executed["status"], "executed")
            self.assertEqual(executed["result"]["eventId"], "evt-123")
            self.assertEqual(len(transport.calls), 1)
            self.assertFalse(pending_path.exists())
            self.assertFalse(decision_path.exists())

            repeated = runtime.calendar_book(**arguments)
            self.assertEqual(repeated["status"], "pending-approval")
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(pending_path.is_file())

            audit = (root / "audit.jsonl").read_text(encoding="utf-8")
            self.assertIn('"outcome":"pending"', audit)
            self.assertIn('"outcome":"approved"', audit)
            self.assertIn('"approver":"owner@example.invalid"', audit)

    def test_changed_write_arguments_do_not_reuse_an_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            transport = FakeCalendarTransport()
            runtime = self._runtime(root, transport)
            first = runtime.calendar_book(
                start="2026-09-16T10:00:00+08:00",
                end="2026-09-16T10:30:00+08:00",
                summary="Original",
            )
            approval_module.decide(
                root / "approvals",
                first["request"]["requestId"],
                approved=True,
                approver="owner",
            )

            changed = runtime.calendar_book(
                start="2026-09-16T10:00:00+08:00",
                end="2026-09-16T10:30:00+08:00",
                summary="Changed",
            )

            self.assertEqual(changed["status"], "pending-approval")
            self.assertNotEqual(first["request"]["requestId"], changed["request"]["requestId"])
            self.assertEqual(transport.calls, [])

    def test_connector_secret_is_required_only_by_governed_tool_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
            desired_path = root / "desired.json"
            desired_path.write_text(json.dumps(desired), encoding="utf-8")
            runtime = runtime_module.GovernedToolRuntime(
                desired_path,
                root / "audit.jsonl",
                root / "approvals",
                environ={},
            )
            with self.assertRaisesRegex(RuntimeError, "credential is not configured"):
                runtime.calendar_availability(
                    start="2026-09-16T10:00:00+08:00",
                    end="2026-09-16T10:30:00+08:00",
                )


if __name__ == "__main__":
    unittest.main()
