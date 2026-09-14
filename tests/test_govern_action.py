from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "govern-action.py"
spec = importlib.util.spec_from_file_location("govern_action", MODULE_PATH)
govern = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(govern)


def desired_state(*, approval_required: bool = True) -> dict:
    return {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "tools": [
            {
                "apiVersion": "modelgarden.ai/v1",
                "kind": "Tool",
                "metadata": {"name": "calendar.book", "owner": "ai-platform"},
                "spec": {
                    "type": "internal",
                    "risk": "write",
                    "approvalRequired": approval_required,
                },
            }
        ],
    }


class GovernActionTests(unittest.TestCase):
    def test_request_id_is_deterministic_and_argument_sensitive(self):
        state = desired_state()
        first = govern.build_action_request(
            state,
            "calendar.book",
            {"slot": "2026-09-15T10:00:00+08:00", "name": "Alice"},
        )
        reordered = govern.build_action_request(
            state,
            "calendar.book",
            {"name": "Alice", "slot": "2026-09-15T10:00:00+08:00"},
        )
        changed = govern.build_action_request(
            state,
            "calendar.book",
            {"name": "Alice", "slot": "2026-09-15T11:00:00+08:00"},
        )
        self.assertEqual(first["requestId"], reordered["requestId"])
        self.assertNotEqual(first["requestId"], changed["requestId"])

    def test_unselected_tool_is_denied(self):
        decision = govern.authorize_action(desired_state(), "crm.write", {"lead": "x"})
        self.assertEqual(decision["decision"], "deny")
        self.assertEqual(decision["reason"], "tool-not-selected")

    def test_selected_approval_required_tool_waits_without_execution(self):
        calls: list[dict] = []
        result = govern.execute_action(
            desired_state(),
            "calendar.book",
            {"slot": "2026-09-15T10:00:00+08:00"},
            calls.append,
        )
        self.assertEqual(result["status"], "pending-approval")
        self.assertEqual(calls, [])

    def test_approval_binds_to_exact_material_action(self):
        state = desired_state()
        original = govern.build_action_request(
            state,
            "calendar.book",
            {"slot": "2026-09-15T10:00:00+08:00"},
        )
        approval = {
            "requestId": original["requestId"],
            "approved": True,
            "approver": "owner@example.test",
        }
        with self.assertRaises(PermissionError):
            govern.execute_action(
                state,
                "calendar.book",
                {"slot": "2026-09-15T11:00:00+08:00"},
                lambda args: args,
                approval=approval,
            )

    def test_exact_approved_action_executes_and_is_audited(self):
        state = desired_state()
        args = {"slot": "2026-09-15T10:00:00+08:00", "caller": "+15550001"}
        request = govern.build_action_request(state, "calendar.book", args)
        approval = {
            "requestId": request["requestId"],
            "approved": True,
            "approver": "practice-manager",
        }
        calls: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = pathlib.Path(tmp) / "audit.jsonl"
            result = govern.execute_action(
                state,
                "calendar.book",
                args,
                lambda payload: calls.append(payload) or {"bookingId": "booking-123"},
                approval=approval,
                initiating_user="caller-session-7",
                audit_path=audit_path,
            )
            events = [json.loads(line) for line in audit_path.read_text().splitlines()]

        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["result"], {"bookingId": "booking-123"})
        self.assertEqual(calls, [args])
        self.assertEqual([event["event"] for event in events], ["authorization", "approval", "execution"])
        self.assertEqual(events[1]["details"]["approver"], "practice-manager")
        self.assertTrue(all(event["requestId"] == request["requestId"] for event in events))
        self.assertTrue(all(event["agent"] == "receptionist" for event in events))

    def test_selected_non_approval_tool_executes(self):
        result = govern.execute_action(
            desired_state(approval_required=False),
            "calendar.book",
            {"slot": "2026-09-15T10:00:00+08:00"},
            lambda args: "ok",
        )
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["result"], "ok")


if __name__ == "__main__":
    unittest.main()
