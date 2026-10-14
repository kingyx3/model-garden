from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ms = load("test_microsoft_graph_calendar", ROOT / "platform/connectors/microsoft_calendar.py")
compiler = load("test_compiler_m365", ROOT / "scripts/compile-workspace.py")
approval = load("test_approver_m365", ROOT / "scripts/runtime-approval.py")
governed = load("test_governed_m365", ROOT / "platform/runtime/governed_mcp.py")


class FakeGraph:
    def __init__(self, *, busy: bool = False, invalid: bool = False):
        self.calls: list[tuple] = []
        self.busy = busy
        self.invalid = invalid

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        if "/getSchedule" in url:
            if self.invalid:
                return (200, {"value": []})
            items = [{"status": "tentative", "start": {}, "end": {}}] if self.busy else []
            return (200, {"value": [{"scheduleId": "frontdesk@example.com", "scheduleItems": items}]})
        return (201, {"id": "event-m365", "webLink": "https://outlook.office.com/calendar/item"})


class MicrosoftCalendarTests(unittest.TestCase):
    def test_availability_maps_graph_getschedule_and_denies_tentative(self):
        fake = FakeGraph(busy=True)
        client = ms.MicrosoftCalendarClient("token-secret", mailbox="frontdesk@example.com", transport=fake)
        got = client.check_availability(start="2026-10-14T10:00:00+08:00", end="2026-10-14T10:30:00+08:00")
        self.assertFalse(got["available"])
        method, url, headers, payload = fake.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/users/frontdesk%40example.com/calendar/getSchedule", url)
        self.assertEqual(payload["startTime"], {"dateTime": "2026-10-14T02:00:00", "timeZone": "UTC"})
        self.assertEqual(payload["schedules"], ["frontdesk@example.com"])
        self.assertEqual(headers["Authorization"], "Bearer token-secret")
        self.assertNotIn("token-secret", json.dumps(payload))
        fake.busy = False
        self.assertTrue(client.check_availability(start="2026-10-14T10:00:00+08:00", end="2026-10-14T10:30:00+08:00")["available"])

    def test_booking_uses_mailbox_specific_graph_event_and_invitation_shape(self):
        fake = FakeGraph()
        client = ms.MicrosoftCalendarClient("token-secret", mailbox="frontdesk@example.com", transport=fake)
        result = client.book({
            "start": "2026-10-14T10:00:00+08:00", "end": "2026-10-14T10:30:00+08:00",
            "summary": "Consultation", "description": "A safe agenda",
            "attendees": ["client@example.net"],
        })
        self.assertEqual(result["eventId"], "event-m365")
        method, url, _, payload = fake.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/users/frontdesk%40example.com/events"))
        self.assertEqual(payload["subject"], "Consultation")
        self.assertEqual(payload["start"]["timeZone"], "UTC")
        self.assertEqual(payload["attendees"], [{"emailAddress": {"address": "client@example.net"}, "type": "required"}])

    def test_validation_and_schedule_failure_fail_closed(self):
        fake = FakeGraph(invalid=True)
        with self.assertRaisesRegex(ValueError, "mailbox"):
            ms.MicrosoftCalendarClient("secret", mailbox="../other", transport=fake)
        with self.assertRaisesRegex(ValueError, "timezone offset"):
            ms.MicrosoftCalendarClient("secret", mailbox="frontdesk@example.com", transport=fake).check_availability(
                start="2026-10-14T10:00:00", end="2026-10-14T10:30:00+08:00")
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            ms.MicrosoftCalendarClient("secret", mailbox="frontdesk@example.com", transport=fake).check_availability(
                start="2026-10-14T10:00:00+08:00", end="2026-10-14T10:30:00+08:00")
        with self.assertRaisesRegex(ValueError, "fixed HTTPS"):
            ms._transport("POST", "https://evil.example/v1.0", {}, {})
        with self.assertRaisesRegex(RuntimeError, "failed \\(403\\)"):
            ms.MicrosoftCalendarClient("secret", mailbox="frontdesk@example.com",
                                        transport=lambda *args: (403, {"error": {"message": "sensitive"}})).book({
                "start": "2026-10-14T10:00:00+08:00", "end": "2026-10-14T10:30:00+08:00"})

    def test_same_governed_tools_and_exact_single_use_approval_for_microsoft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
            desired_path = root / "desired.json"
            desired_path.write_text(json.dumps(desired), encoding="utf-8")
            fake = FakeGraph()
            runtime = governed.GovernedToolRuntime(
                desired_path, root / "audit.jsonl", root / "approvals",
                environ={
                    "MODEL_GARDEN_CALENDAR_PROVIDER": "microsoft",
                    "MODEL_GARDEN_MICROSOFT_CALENDAR_TOKEN": "secret-graph",
                    "MODEL_GARDEN_MICROSOFT_CALENDAR_MAILBOX": "frontdesk@example.com",
                },
                calendar_transport=fake,
            )
            start, end = "2026-10-14T10:00:00+08:00", "2026-10-14T10:30:00+08:00"
            read = runtime.calendar_availability(start=start, end=end)
            self.assertEqual(read["status"], "executed")
            args = {"start": start, "end": end, "summary": "Service appointment"}
            pending = runtime.calendar_book(**args)
            self.assertEqual(pending["status"], "pending-approval")
            self.assertEqual(len(fake.calls), 1)
            approval.decide(root / "approvals", pending["request"]["requestId"],
                            approved=True, approver="human-owner@example.com")
            done = runtime.calendar_book(**args)
            self.assertEqual(done["status"], "executed")
            self.assertEqual(done["result"]["eventId"], "event-m365")
            self.assertEqual(runtime.calendar_book(**args)["status"], "pending-approval")
            self.assertEqual(len(fake.calls), 2)
            audit = (root / "audit.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("secret-graph", audit)
            self.assertNotIn("client@example.net", audit)

    def test_selected_provider_must_have_matching_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
            path = root / "desired.json"
            path.write_text(json.dumps(desired), encoding="utf-8")
            runtime = governed.GovernedToolRuntime(
                path, root / "audit.jsonl", root / "approvals",
                environ={"MODEL_GARDEN_CALENDAR_PROVIDER": "microsoft",
                         "MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN": "google-is-not-enough"},
            )
            with self.assertRaisesRegex(RuntimeError, "Microsoft Calendar credential"):
                runtime.calendar_availability(start="2026-10-14T10:00:00Z", end="2026-10-14T10:30:00Z")


if __name__ == "__main__":
    unittest.main()
