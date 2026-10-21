"""Credential-free checks for the built-in inbox: one non-sensitive view of what waits for a person."""
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


inbox = _load("inbox_under_test", "platform/core/inbox.py")
handoff = _load("handoff_for_inbox", "platform/core/handoff.py")
contacts = _load("contacts_for_inbox", "platform/core/contacts.py")
crm_sync = _load("crm_sync_for_inbox", "platform/core/crm_sync.py")
cli = _load("runtime_inbox_under_test", "scripts/runtime-inbox.py")
operator = _load("client_operator_for_inbox", "scripts/client-operator.py")

NOW = 1_790_000_000
SECRET_ARGUMENT = "Hargreaves boiler 12 Acacia Road"


def request_id(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


class InboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.approvals, self.handoffs, self.contacts = self.root / "approvals", self.root / "handoffs", self.root / "contacts"

    def approval(self, seed: str, tool: str = "calendar.book", *, age: int = 600) -> str:
        identifier = request_id(seed)
        path = self.approvals / "pending" / f"{identifier}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"requestId": identifier, "tool": tool, "arguments": {"note": SECRET_ARGUMENT}}), encoding="utf-8")
        import os

        os.utime(path, (NOW - age, NOW - age))
        return identifier

    def hand(self, seed: str, *, assignee="jordan@client.example", hours=24, created=NOW - 3600) -> str:
        identifier = request_id(seed)
        handoff.create(
            self.handoffs, {"requestId": identifier, "tool": "calendar.book", "arguments": {"note": SECRET_ARGUMENT}},
            capability="calendar", reason="unbound", assignee=assignee, deadline_hours=hours, now=created,
        )
        return identifier

    def collect(self):
        return inbox.collect(approvals=self.approvals, handoffs=self.handoffs, contacts=self.contacts, now=NOW)


class TicketsInInboxTests(InboxCase):
    def test_open_tickets_show_as_a_count_and_age_and_never_as_text(self):
        tickets = _load("tickets_for_inbox", "platform/core/tickets.py")
        store = tickets.TicketStore(self.root / "tickets", tenant="acme")
        made = store.create(project="support", title=SECRET_ARGUMENT, description=SECRET_ARGUMENT, requested_by="unmapped:slack:x", now=NOW - 3600)
        store.create(project="support", title="Second", requested_by="unmapped:slack:x", now=NOW - 60)
        closed = store.create(project="support", title="Closed", requested_by="unmapped:slack:x", now=NOW - 10)
        store.update(ticket_id=closed["ticketId"], status="done", requested_by="jordan@client.example", principal_access="member")
        result = inbox.collect(approvals=self.approvals, handoffs=self.handoffs, tickets=self.root / "tickets", now=NOW)
        self.assertEqual(result["counts"]["tickets"], 2)
        (item,) = [item for item in result["items"] if item["kind"] == "tickets"]
        self.assertEqual((item["count"], item["oldestWaitingSeconds"]), (2, 3600))
        self.assertNotIn("Hargreaves", json.dumps(result))
        self.assertIn("2 open, oldest waiting 1 h", inbox.format_text(result))
        self.assertNotIn(made["ticketId"], json.dumps(result))
        self.assertEqual(inbox.collect(approvals=self.approvals, handoffs=self.handoffs, now=NOW)["counts"]["tickets"], 0)


class CollectTests(InboxCase):
    def test_an_empty_runtime_says_so(self):
        result = self.collect()
        self.assertEqual((result["items"], result["counts"]), ([], {"paused": 0, "approvals": 0, "handoffs": 0, "overdueHandoffs": 0, "reviews": 0, "tickets": 0, "crmFilings": 0, "connections": 0}))
        self.assertEqual(inbox.format_text(result), "Nothing is waiting for a person.\n")

    def test_items_are_ordered_overdue_handoffs_then_oldest_approvals_then_open_handoffs_then_reviews(self):
        late = self.hand("late", hours=1, created=NOW - 7200)
        soon = self.hand("soon", hours=48)
        old, recent = self.approval("old", age=7200), self.approval("recent", age=60)
        store = contacts.ContactStore(self.contacts)
        store.create_contact({"email": "twin@client.example"})
        store.create_contact({"email": "twin@client.example"})
        store.log_interaction({"objectId": "unmatched", "interactionType": "call", "direction": "inbound", "channel": "voice",
                               "timestamp": "2026-10-20T10:00:00+08:00", "summary": SECRET_ARGUMENT, "transcriptRef": "transcript:x"})
        result = self.collect()
        self.assertEqual([(item["kind"], item.get("id")) for item in result["items"][:4]],
                         [("handoff", late), ("approval", old), ("approval", recent), ("handoff", soon)])
        self.assertEqual([item["what"] for item in result["items"][4:]], ["unmatched-interactions", "possible-duplicate-contacts"])
        self.assertEqual(result["counts"], {"paused": 0, "approvals": 2, "handoffs": 2, "overdueHandoffs": 1, "reviews": 2, "tickets": 0, "crmFilings": 0, "connections": 0})
        self.assertTrue(result["items"][0]["overdue"])
        self.assertFalse(result["items"][3]["overdue"])

    def test_nothing_sensitive_is_ever_copied_into_the_inbox(self):
        self.hand("a")
        self.approval("b")
        store = contacts.ContactStore(self.contacts)
        store.create_contact({"name": "Alex Tan", "phone": "+65 6123 4567"})
        store.log_interaction({"objectId": "unmatched", "interactionType": "call", "direction": "inbound", "channel": "voice",
                               "timestamp": "2026-10-20T10:00:00+08:00", "summary": SECRET_ARGUMENT})
        result = self.collect()
        rendered = json.dumps(result) + inbox.format_text(result)
        for private in ("Hargreaves", "Acacia", "Alex", "6123 4567", "61234567", "arguments"):
            self.assertNotIn(private, rendered)
        self.assertIn("jordan@client.example", rendered)  # the assignee is the point of a hand-off

    def test_closed_handoffs_and_decided_approvals_leave_the_inbox(self):
        identifier = self.hand("a")
        handoff.complete(self.handoffs, identifier, "done", "jordan@client.example", now=NOW)
        (self.approvals / "pending").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.collect()["items"], [])

    def test_an_expired_approval_is_not_listed_because_it_can_no_longer_be_decided(self):
        self.approval("fresh", age=3600)
        self.approval("stale", age=30 * 3600)
        self.assertEqual([item["id"] for item in self.collect()["items"]], [request_id("fresh")])

    def blocked_filing(self, external_id="thread-1", *, created=NOW - 7200, error="unauthorized", tenant="acme-prod"):
        ledger = crm_sync.SyncLedger(self.root / "crm-sync", tenant=tenant)
        key = ledger.enqueue(channel="email", external_id=external_id, interaction_type="email", now=created)["key"]
        ledger.record_failure(key, error_code=error, now=created)
        return key

    def test_a_blocked_crm_filing_waits_in_the_inbox_as_a_count_and_an_age_never_content(self):
        self.blocked_filing("thread-1", created=NOW - 7200)
        self.blocked_filing("thread-2", created=NOW - 600)
        failed = crm_sync.SyncLedger(self.root / "crm-sync", tenant="acme-prod")
        retrying = failed.enqueue(channel="email", external_id="thread-3", interaction_type="email", now=NOW)["key"]
        failed.record_failure(retrying, error_code="outage", now=NOW)  # transient: the worker retries it, nobody is asked
        done = failed.enqueue(channel="email", external_id="thread-4", interaction_type="email", now=NOW)["key"]
        failed.record_success(done, provider_ref="built-in-crm:interaction/i-1", now=NOW)
        result = inbox.collect(approvals=self.approvals, handoffs=self.handoffs, crm_sync=self.root / "crm-sync", now=NOW)
        (item,) = result["items"]
        self.assertEqual((item["kind"], item["count"], item["oldestWaitingSeconds"]), ("crm-filing", 2, 7200))
        self.assertEqual(result["counts"]["crmFilings"], 2)
        self.assertIn("crm-sync list", item["next"])
        text = inbox.format_text(result)
        self.assertIn("2 CRM filing(s) blocked", text)
        chat = inbox.format_chat(result)
        self.assertIn("2 customer email(s) did not reach the CRM", chat)
        self.assertNotIn("thread-1", text + chat + json.dumps(result))

    def test_no_blocked_filing_or_no_store_adds_nothing_and_a_member_does_not_see_it(self):
        self.assertEqual(inbox.collect(approvals=self.approvals, handoffs=self.handoffs, crm_sync=self.root / "crm-sync", now=NOW)["items"], [])
        self.blocked_filing()
        snapshot = inbox.collect(approvals=self.approvals, handoffs=self.handoffs, crm_sync=self.root / "crm-sync", now=NOW)
        self.assertEqual(inbox.for_person(snapshot, "someone@acme.example", everything=False)["items"], [])
        self.assertEqual(len(inbox.for_person(snapshot, "approver@acme.example", everything=True)["items"]), 1)

    def test_unsafe_or_corrupt_records_are_skipped_rather_than_crashing_the_view(self):
        self.approval("good")
        folder = self.approvals / "pending"
        (folder / "garbage.json").write_text("not json", encoding="utf-8")
        (folder / f"{request_id('other')}.json").write_text(json.dumps({"requestId": "different"}), encoding="utf-8")
        (folder / "list.json").write_text("[]", encoding="utf-8")
        self.assertEqual([item["kind"] for item in self.collect()["items"]], ["approval"])

    def test_text_output_names_the_next_command_for_each_item(self):
        self.hand("late", hours=1, created=NOW - 7200)
        self.approval("a", tool="mail.send")
        text = inbox.format_text(self.collect())
        self.assertIn("1 approval(s), 1 hand-off(s) (1 overdue)", text)
        self.assertIn("OVERDUE", text)
        self.assertIn("mail.send", text)
        self.assertIn("client-operator.py handoff-done", text)
        self.assertIn("client-operator.py approve", text)


class CommandTests(InboxCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--data-root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_the_runtime_command_prints_text_or_json(self):
        self.hand("a")
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("HAND-OFF", out)
        code, out, _ = self.run_cli("--json")
        self.assertEqual(json.loads(out)["counts"]["handoffs"], 1)

    def test_the_runtime_command_reads_the_workers_crm_sync_directory(self):
        crm = crm_sync.SyncLedger(self.root / "crm-sync", tenant="acme-prod")
        key = crm.enqueue(channel="email", external_id="thread-9", interaction_type="email", now=NOW - 60)["key"]
        crm.record_failure(key, error_code="rejected", now=NOW - 60)
        code, out, _ = self.run_cli("--json")
        self.assertEqual((code, json.loads(out)["counts"]["crmFilings"]), (0, 1))

    def test_an_empty_data_root_is_a_clean_no_op(self):
        code, out, _ = self.run_cli()
        self.assertEqual((code, out), (0, "Nothing is waiting for a person.\n"))

    def test_the_operator_command_runs_the_runtime_command_in_the_governed_container(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return mock.Mock(returncode=0, stdout="gov1\n", stderr="")
            return mock.Mock(returncode=0, stdout="Nothing is waiting for a person.\n", stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operator.runtime_inbox("acme-dev", as_json=True), 0)
        self.assertEqual(calls[-1][:3], ["docker", "exec", "gov1"])
        self.assertTrue(calls[-1][-2].endswith("runtime-inbox.py") and calls[-1][-1] == "--json")
        with self.assertRaises(ValueError):
            operator.runtime_inbox("Bad Name")


if __name__ == "__main__":
    unittest.main()
