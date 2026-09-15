from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONNECTOR_PATH = ROOT / "platform" / "connectors" / "google_calendar.py"
GOVERN_PATH = ROOT / "scripts" / "govern-action.py"

connector_spec = importlib.util.spec_from_file_location("google_calendar", CONNECTOR_PATH)
calendar = importlib.util.module_from_spec(connector_spec)
assert connector_spec.loader is not None
connector_spec.loader.exec_module(calendar)

govern_spec = importlib.util.spec_from_file_location("govern_action", GOVERN_PATH)
govern = importlib.util.module_from_spec(govern_spec)
assert govern_spec.loader is not None
govern_spec.loader.exec_module(govern)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        return self.responses.pop(0)


def desired_state():
    return {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "agent": {"spec": {"approvals": {"externalActions": "none"}}},
        "tools": [
            {
                "metadata": {"name": "calendar.book"},
                "spec": {"approvalRequired": True, "risk": "write", "type": "internal"},
            }
        ],
    }


class GoogleCalendarTests(unittest.TestCase):
    def test_availability_uses_google_freebusy_api_without_leaking_token(self):
        transport = FakeTransport([
            (200, {"calendars": {"primary": {"busy": []}}}),
        ])
        client = calendar.GoogleCalendarClient("runtime-secret", transport=transport)
        result = client.check_availability(
            start="2026-09-15T10:00:00+08:00",
            end="2026-09-15T10:30:00+08:00",
            timezone="Asia/Singapore",
        )
        self.assertTrue(result["available"])
        method, url, headers, payload = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/freeBusy"))
        self.assertEqual(headers["Authorization"], "Bearer runtime-secret")
        self.assertNotIn("runtime-secret", repr(payload))

    def test_busy_interval_is_not_available(self):
        transport = FakeTransport([
            (200, {"calendars": {"primary": {"busy": [{"start": "2026-09-15T10:00:00+08:00", "end": "2026-09-15T10:30:00+08:00"}]}}}),
        ])
        client = calendar.GoogleCalendarClient("token", transport=transport)
        result = client.check_availability(
            start="2026-09-15T10:00:00+08:00",
            end="2026-09-15T10:30:00+08:00",
        )
        self.assertFalse(result["available"])

    def test_exact_approved_governed_booking_creates_google_event(self):
        transport = FakeTransport([
            (
                200,
                {
                    "id": "evt-123",
                    "status": "confirmed",
                    "htmlLink": "https://calendar.google.test/event?eid=evt-123",
                    "start": {"dateTime": "2026-09-15T10:00:00+08:00"},
                    "end": {"dateTime": "2026-09-15T10:30:00+08:00"},
                },
            )
        ])
        client = calendar.GoogleCalendarClient("runtime-secret", transport=transport)
        args = {
            "start": "2026-09-15T10:00:00+08:00",
            "end": "2026-09-15T10:30:00+08:00",
            "summary": "New patient consultation",
            "attendees": ["caller@example.test"],
        }
        request = govern.build_action_request(desired_state(), "calendar.book", args, initiating_user="caller-1")
        result = govern.execute_action(
            desired_state(),
            "calendar.book",
            args,
            client.booking_executor(),
            initiating_user="caller-1",
            approval={"requestId": request["requestId"], "approved": True, "approver": "owner@example.test"},
        )
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["result"]["eventId"], "evt-123")
        method, url, headers, payload = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/calendars/primary/events", url)
        self.assertEqual(payload["summary"], "New patient consultation")
        self.assertEqual(payload["attendees"], [{"email": "caller@example.test"}])
        self.assertNotIn("runtime-secret", repr(payload))

    def test_governed_booking_audit_correlates_conversation_without_storing_provider_result(self):
        transport = FakeTransport([(200, {"id": "evt-sensitive", "status": "confirmed", "htmlLink": "https://calendar.google.test/private"})])
        client = calendar.GoogleCalendarClient("runtime-secret", transport=transport)
        args = {
            "start": "2026-09-15T10:00:00+08:00",
            "end": "2026-09-15T10:30:00+08:00",
            "summary": "Consultation",
        }
        governance_context = {
            "tenantId": "demo-dental",
            "conversationId": "call-42",
            "runtime": "hermes",
            "runtimeVersion": "0.21.2",
            "modelProvider": "openai",
            "model": "approved-model",
            "modelConfigVersion": "profile-sha-123",
            "credentialScope": "calendar.events.write",
        }
        request = govern.build_action_request(
            desired_state(), "calendar.book", args, initiating_user="caller-1", governance_context=governance_context
        )
        approval = {"requestId": request["requestId"], "approved": True, "approver": "practice-manager"}
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = pathlib.Path(tmp) / "audit.jsonl"
            result = govern.execute_action(
                desired_state(),
                "calendar.book",
                args,
                client.booking_executor(),
                initiating_user="caller-1",
                governance_context=governance_context,
                approval=approval,
                audit_path=audit_path,
            )
            audit_text = audit_path.read_text()
            events = [json.loads(line) for line in audit_text.splitlines()]

        self.assertEqual(result["result"]["eventId"], "evt-sensitive")
        self.assertEqual([event["event"] for event in events], ["authorization", "approval", "execution"])
        self.assertTrue(all(event["governance"]["conversationId"] == "call-42" for event in events))
        self.assertTrue(all(event["governance"]["tenantId"] == "demo-dental" for event in events))
        self.assertEqual(events[1]["details"]["approver"], "practice-manager")
        self.assertIn("resultDigest", events[2]["details"])
        self.assertNotIn("evt-sensitive", audit_text)
        self.assertNotIn("calendar.google.test/private", audit_text)
        self.assertNotIn("runtime-secret", audit_text)

    def test_booking_requires_timezone_aware_bounded_window(self):
        client = calendar.GoogleCalendarClient("token", transport=FakeTransport([]))
        with self.assertRaises(ValueError):
            client.book({"start": "2026-09-15T10:00:00", "end": "2026-09-15T10:30:00+08:00"})
        with self.assertRaises(ValueError):
            client.book({"start": "2026-09-15T10:30:00+08:00", "end": "2026-09-15T10:00:00+08:00"})

    def test_provider_error_fails_closed(self):
        transport = FakeTransport([(403, {"error": {"message": "insufficient permissions"}})])
        client = calendar.GoogleCalendarClient("token", transport=transport)
        with self.assertRaisesRegex(RuntimeError, "insufficient permissions"):
            client.check_availability(
                start="2026-09-15T10:00:00+08:00",
                end="2026-09-15T10:30:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
