"""Credential-free checks for Tier 4 hand-off notices: assigned, reminder and escalation, recorded once each."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


handoff = _load("handoff_for_notices", "platform/core/handoff.py")
govern = _load("govern_for_notices", "scripts/govern-action.py")
cli = _load("runtime_handoff_for_notices", "scripts/runtime-handoff.py")
operator = _load("client_operator_for_notices", "scripts/client-operator.py")

T0 = 1_790_000_000
HOUR = 3600
SECRET = "Hargreaves boiler at 12 Acacia Road"


def request(index: int = 1) -> dict:
    return {"agent": "receptionist", "tool": "calendar.book", "requestId": f"{index:064x}",
            "arguments": {"summary": SECRET, "start": "2026-10-20T10:00:00+08:00"}}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "handoffs"

    def create(self, index=1, *, hours=24, assignee="jordan@client.example"):
        return handoff.create(self.root, request(index), capability="calendar", reason="unbound",
                              assignee=assignee, deadline_hours=hours, now=T0)

    def due(self, at, **kwargs):
        return handoff.due_notices(self.root, now=T0 + at, **kwargs)

    def kinds(self, at, **kwargs):
        return [(item["kind"], item["to"]) for item in self.due(at, **kwargs)]


class DueTests(Case):
    def test_a_new_handoff_first_produces_an_assigned_notice_for_the_named_person(self):
        self.create()
        self.assertEqual(self.kinds(0), [("assigned", "jordan@client.example")])

    def test_the_reminder_comes_a_quarter_of_the_window_before_the_deadline_within_30_minutes_to_2_hours(self):
        self.create(hours=24)  # a quarter would be 6 hours; capped at 2
        handoff.mark_notified(self.root, request()["requestId"], "assigned", via="email", now=T0)
        self.assertEqual(self.kinds(22 * HOUR - 1), [])
        self.assertEqual(self.kinds(22 * HOUR), [("reminder", "jordan@client.example")])
        self.root = self.root.parent / "short"
        self.create(hours=1)  # a quarter would be 15 minutes; floored at 30
        handoff.mark_notified(self.root, request()["requestId"], "assigned", via="email", now=T0)
        self.assertEqual(self.kinds(HOUR // 2 - 1), [])
        self.assertEqual(self.kinds(HOUR // 2), [("reminder", "jordan@client.example")])

    def test_once_overdue_there_is_an_escalation_to_the_named_contact_and_no_late_reminder(self):
        self.create(hours=2)
        for kind in ("assigned", "reminder"):
            handoff.mark_notified(self.root, request()["requestId"], kind, via="email", now=T0)
        self.assertEqual(self.kinds(2 * HOUR), [])  # the deadline itself is not yet late
        self.assertEqual(self.kinds(2 * HOUR + 1), [("escalation", "jordan@client.example")])
        self.assertEqual(self.kinds(2 * HOUR + 1, escalate_to="owner@client.example"), [("escalation", "owner@client.example")])
        notice = self.due(3 * HOUR, escalate_to="owner@client.example")[0]
        self.assertTrue(notice["overdue"])
        self.assertIn("OVERDUE", notice["text"])
        self.assertIn("jordan@client.example", notice["text"])

    def test_a_hand_off_found_overdue_for_the_first_time_gets_assigned_and_escalation_but_no_reminder(self):
        self.create(hours=1)
        self.assertEqual(self.kinds(5 * HOUR), [("assigned", "jordan@client.example"), ("escalation", "jordan@client.example")])

    def test_each_notice_is_offered_until_recorded_then_never_again(self):
        self.create()
        notice = self.due(0)[0]
        self.assertEqual(self.due(0), [notice])  # not recorded: offered again (at-least-once)
        handoff.mark_notified(self.root, notice["handoffId"], "assigned", via="slack", now=T0)
        self.assertEqual(self.due(0), [])
        again = handoff.mark_notified(self.root, notice["handoffId"], "assigned", via="email", now=T0 + 99)
        self.assertEqual(again["notices"]["assigned"], {"at": T0, "via": "slack"})  # the first record stands

    def test_closed_hand_offs_produce_no_notices(self):
        self.create()
        handoff.complete(self.root, request()["requestId"], "done", "jordan@client.example", now=T0 + 10)
        self.assertEqual(self.due(30 * HOUR), [])

    def test_notices_never_carry_the_action_arguments_or_personal_data(self):
        self.create(hours=1)
        rendered = json.dumps(self.due(5 * HOUR))
        for private in ("Hargreaves", "Acacia", "2026-10-20", "arguments", "summary"):
            self.assertNotIn(private, rendered)
        self.assertIn("calendar.book", rendered)
        self.assertIn(request()["requestId"][:12], rendered)

    def test_several_hand_offs_are_independent(self):
        self.create(1)
        self.create(2, assignee="sam@client.example")
        handoff.mark_notified(self.root, request(1)["requestId"], "assigned", via="email", now=T0)
        self.assertEqual(self.kinds(0), [("assigned", "sam@client.example")])

    def test_bad_input_is_refused(self):
        self.create()
        with self.assertRaises(handoff.HandoffError):
            handoff.mark_notified(self.root, request()["requestId"], "shout", via="email")
        with self.assertRaises(handoff.HandoffError):
            handoff.mark_notified(self.root, request()["requestId"], "assigned", via=" ")
        with self.assertRaises(handoff.HandoffError):
            handoff.mark_notified(self.root, "nope", "assigned", via="email")
        with self.assertRaises(handoff.HandoffError):
            handoff.mark_notified(self.root, request(9)["requestId"], "assigned", via="email")
        with self.assertRaises(handoff.HandoffError):
            handoff.due_notices(self.root, now=T0, escalate_to="  ")

    def test_marking_keeps_the_record_private_and_the_hand_off_otherwise_intact(self):
        before = self.create()
        after = handoff.mark_notified(self.root, before["id"], "assigned", via="email", now=T0)
        self.assertEqual({k: v for k, v in after.items() if k != "notices"}, before)
        path = self.root / "pending" / f"{before['id']}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class CommandTests(Case):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_due_then_notified_through_the_runtime_command_with_a_ledger_event(self):
        self.create(hours=1)
        code, out, _ = self.run_cli("due", "--escalate-to", "owner@client.example")
        notices = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn("assigned", [n["kind"] for n in notices])  # the clock here is real time, so only this is certain
        identifier = request()["requestId"]
        code, out, _ = self.run_cli("notified", identifier, "assigned", "--via", "email")
        self.assertEqual((code, json.loads(out)["via"]), (0, "email"))
        self.assertNotIn("assigned", [n["kind"] for n in json.loads(self.run_cli("due")[1])])
        events = [json.loads(line) for line in (self.root.parent / "audit.jsonl").read_text().splitlines()]
        notified = [e for e in events if e.get("action") == "handoff.notified"]
        self.assertEqual(len(notified), 1)
        self.assertEqual((notified[0]["performedBy"], notified[0]["details"]["kind"]), ("runtime:handoff-notifier", "assigned"))
        self.assertTrue(govern.verify_audit_chain(self.root.parent / "audit.jsonl")["valid"])
        self.assertNotIn("Hargreaves", (self.root.parent / "audit.jsonl").read_text())

    def test_a_bad_kind_or_unknown_hand_off_fails_without_a_ledger_event(self):
        self.create()
        code, _, err = self.run_cli("notified", request(7)["requestId"], "assigned", "--via", "email")
        self.assertEqual(code, 1)
        self.assertIn("Runtime hand-off failed", err)
        self.assertFalse((self.root.parent / "audit.jsonl").exists())

    def test_the_operator_commands_validate_before_touching_docker_and_pass_the_right_arguments(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return mock.Mock(returncode=0, stdout="gov1\n", stderr="")
            return mock.Mock(returncode=0, stdout="[]\n", stderr="")

        identifier = request()["requestId"]
        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operator.runtime_handoff_notices("acme-dev", "due", escalate_to="owner@client.example"), 0)
            self.assertEqual(operator.runtime_handoff_notices("acme-dev", "notified", handoff_id=identifier, kind="reminder", via="slack"), 0)
        due, notified = calls[1], calls[3]
        self.assertEqual(due[-3:], ["due", "--escalate-to", "owner@client.example"])
        self.assertEqual(notified[-4:], [identifier, "reminder", "--via", "slack"])
        before = len(calls)
        for kwargs in ({"handoff_id": "short", "kind": "reminder", "via": "slack"},
                       {"handoff_id": identifier, "kind": "shout", "via": "slack"},
                       {"handoff_id": identifier, "kind": "reminder", "via": "Slack; rm"}):
            with self.assertRaises(ValueError):
                operator.runtime_handoff_notices("acme-dev", "notified", **kwargs)
        with self.assertRaises(ValueError):
            operator.runtime_handoff_notices("acme-dev", "send")
        self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()
