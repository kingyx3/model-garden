"""Credential-free checks for delivering hand-off and connection notices through the named person's own channel."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


slack = _load("slack_for_delivery", "platform/channels/slack_channel.py")
delivery = _load("notice_delivery_under_test", "platform/core/notice_delivery.py")
handoff = _load("handoff_for_delivery", "platform/core/handoff.py")
notices = _load("connection_notices_for_delivery", "platform/core/connection_notices.py")
T0 = 1_790_000_000
JORDAN, PRIYA = "jordan@client.example", "priya@client.example"
SECRET = "Hargreaves boiler at 12 Acacia Road"


class Recorder:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_for = fail_for or set()

    def __call__(self, address: str, text: str) -> str:
        if address in self.fail_for:
            raise ConnectionError("provider said: " + SECRET)
        self.sent.append((address, text))
        return "ref"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "handoffs"
        self.events: list[tuple] = []
        self.audit = lambda *event: self.events.append(event)

    def create(self, index=1, assignee=JORDAN):
        request = {"agent": "receptionist", "tool": "calendar.book", "requestId": f"{index:064x}",
                   "arguments": {"summary": SECRET, "start": "2026-10-20T10:00:00+08:00"}}
        return handoff.create(self.root, request, capability="calendar", reason="unbound", assignee=assignee, deadline_hours=24, now=T0)

    def run_handoffs(self, directory, senders, at=0, **kwargs):
        return delivery.deliver([delivery.handoff_source(self.root, now=T0 + at, **kwargs)], directory, senders, audit=self.audit)


class DirectoryTests(unittest.TestCase):
    def test_a_directory_is_strict(self):
        good = delivery.Directory({JORDAN: {"channel": "slack", "address": "D0123ABC"}, PRIYA: {"channel": "inbox"}})
        self.assertEqual((good.lookup(JORDAN), good.lookup(PRIYA), good.lookup("nobody"), good.lookup(None)), (("slack", "D0123ABC"), ("inbox", ""), None, None))
        for bad in ({"bad person!": {"channel": "slack", "address": "D1"}}, {JORDAN: {"channel": "Slack", "address": "D1"}},
                    {JORDAN: {"channel": "slack"}}, {JORDAN: {"channel": "slack", "address": "a\nb"}},
                    {JORDAN: {"channel": "slack", "address": "xoxb-token-1234"}}, {JORDAN: {"channel": "slack", "address": "D1", "token": "x"}},
                    {JORDAN: "slack"}, [JORDAN]):
            with self.assertRaises(delivery.DeliveryError, msg=str(bad)):
                delivery.Directory(bad)  # type: ignore[arg-type]


class HandoffDeliveryTests(Case):
    def test_a_new_handoff_reaches_the_named_person_on_their_own_channel_and_is_then_marked(self):
        self.create()
        slack_sender = Recorder()
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D0123ABC"}})
        results = self.run_handoffs(directory, {"slack": slack_sender})
        self.assertEqual([(r["status"], r["via"], r["to"]) for r in results], [("delivered", "slack", JORDAN)])
        self.assertEqual(len(slack_sender.sent), 1)
        address, text = slack_sender.sent[0]
        self.assertEqual(address, "D0123ABC")
        self.assertIn("calendar.book is waiting for you", text)
        self.assertNotIn("Acacia", text)  # the action's arguments never travel in a notice
        self.assertEqual(handoff.pending(self.root)[0]["notices"]["assigned"]["via"], "slack")
        self.assertEqual(self.run_handoffs(directory, {"slack": slack_sender}), [])  # recorded once, never repeated
        self.assertEqual(len(slack_sender.sent), 1)

    def test_two_people_are_reached_each_on_their_own_channel(self):
        self.create(1, JORDAN)
        self.create(2, PRIYA)
        chat, mail = Recorder(), Recorder()
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D1AAA"}, PRIYA: {"channel": "email", "address": "priya@client.example"}})
        results = self.run_handoffs(directory, {"slack": chat, "email": mail})
        self.assertEqual(sorted(r["via"] for r in results), ["email", "slack"])
        self.assertEqual((len(chat.sent), len(mail.sent)), (1, 1))
        self.assertEqual(mail.sent[0][0], "priya@client.example")

    def test_a_failed_send_leaves_the_notice_due_records_only_the_error_type_and_does_not_stop_the_batch(self):
        self.create(1, JORDAN)
        self.create(2, PRIYA)
        sender = Recorder(fail_for={"D1AAA"})
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D1AAA"}, PRIYA: {"channel": "slack", "address": "D2BBB"}})
        results = {r["to"]: r for r in self.run_handoffs(directory, {"slack": sender})}
        self.assertEqual((results[JORDAN]["status"], results[JORDAN]["error"], results[PRIYA]["status"]), ("failed", "ConnectionError", "delivered"))
        self.assertNotIn("Hargreaves", json.dumps(results) + json.dumps(self.events))
        again = self.run_handoffs(directory, {"slack": Recorder()})
        self.assertEqual([(r["to"], r["status"]) for r in again], [(JORDAN, "delivered")])  # only the failed one is offered again

    def test_a_person_with_no_channel_is_reported_and_stays_due_and_inbox_counts_only_when_chosen(self):
        self.create(1, JORDAN)
        self.create(2, PRIYA)
        directory = delivery.Directory({PRIYA: {"channel": "inbox"}, "other@client.example": {"channel": "teams", "address": "19:abc"}})
        results = {r["to"]: r for r in self.run_handoffs(directory, {})}
        self.assertEqual((results[JORDAN]["status"], results[PRIYA]["status"], results[PRIYA]["via"]), ("no-channel", "delivered", "inbox"))
        self.assertEqual([r["to"] for r in self.run_handoffs(delivery.Directory({}), {})], [JORDAN])
        self.assertIn("assigned", [p for p in handoff.pending(self.root) if p["assignee"] == PRIYA][0]["notices"])
        self.assertNotIn("notices", [p for p in handoff.pending(self.root) if p["assignee"] == JORDAN][0])

    def test_a_registered_name_with_no_sender_is_no_channel_not_a_crash(self):
        self.create()
        directory = delivery.Directory({JORDAN: {"channel": "teams", "address": "19:abc"}})
        self.assertEqual([r["status"] for r in self.run_handoffs(directory, {"slack": Recorder()})], ["no-channel"])

    def test_an_overdue_handoff_escalates_to_the_named_contact_on_their_channel(self):
        self.create()
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D1AAA"}, PRIYA: {"channel": "slack", "address": "D2BBB"}})
        sender = Recorder()
        self.run_handoffs(directory, {"slack": sender})
        sender.sent.clear()
        results = self.run_handoffs(directory, {"slack": sender}, at=25 * 3600, escalate_to=PRIYA)
        escalation = [r for r in results if r["kind"] == "escalation"]
        self.assertEqual([(r["to"], r["status"]) for r in escalation], [(PRIYA, "delivered")])
        self.assertTrue(any(address == "D2BBB" and text.startswith("OVERDUE") for address, text in sender.sent))

    def test_a_failed_mark_after_a_send_is_reported_and_the_next_run_sends_again(self):
        self.create()
        notices_list, _ = delivery.handoff_source(self.root, now=T0)

        def broken(_item, _via):
            raise OSError("read-only")

        sender = Recorder()
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D1AAA"}})
        results = delivery.deliver([(notices_list, broken)], directory, {"slack": sender}, audit=self.audit)
        self.assertEqual(results[0]["status"], "unmarked")
        self.assertEqual([r["status"] for r in self.run_handoffs(directory, {"slack": sender})], ["delivered"])
        self.assertEqual(len(sender.sent), 2)  # duplicated rather than lost

    def test_the_ledger_records_what_happened_but_never_the_text_or_the_address(self):
        self.create()
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D0123ABC"}})
        self.run_handoffs(directory, {"slack": Recorder()})
        text = json.dumps(self.events)
        self.assertEqual([event[0] for event in self.events], ["notice.delivered"])
        self.assertNotIn("D0123ABC", text)
        self.assertNotIn("waiting for you", text)
        self.assertEqual(self.events[0][2]["channel"], "slack")

    def test_an_audit_hook_that_raises_does_not_stop_delivery(self):
        self.create()
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D1AAA"}})

        def broken(*_):
            raise RuntimeError("ledger down")

        results = delivery.deliver([delivery.handoff_source(self.root, now=T0)], directory, {"slack": Recorder()}, audit=broken)
        self.assertEqual(results[0]["status"], "delivered")


class ConnectionDeliveryTests(Case):
    def test_an_owner_is_told_about_a_failed_connection_on_their_channel_once(self):
        store = notices.NoticeStore(pathlib.Path(self._tmp.name) / "governance", audit=self.audit)
        health = {"calendar": {"health": "failed", "updatedAt": "2026-10-16T01:00:00Z"}}
        connectors = {"calendar": {"owner": JORDAN}}
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D1AAA"}})
        sender = Recorder()
        for expected in (1, 1):
            source = delivery.connection_source(store, health, connectors, now=T0)
            delivery.deliver([source], directory, {"slack": sender}, audit=self.audit)
            self.assertEqual(len(sender.sent), expected)
        self.assertIn("calendar connection is failed", sender.sent[0][1])
        self.assertIn("connection.notice-delivered", [event[0] for event in self.events])

    def test_an_unowned_notice_is_reported_every_run_and_never_dropped(self):
        store = notices.NoticeStore(pathlib.Path(self._tmp.name) / "governance")
        health = {"calendar": {"health": "revoked", "updatedAt": "2026-10-16T01:00:00Z"}}
        directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D1AAA"}})
        for _ in range(2):
            results = delivery.deliver([delivery.connection_source(store, health, {"calendar": {}}, now=T0)], directory, {"slack": Recorder()}, audit=self.audit)
            self.assertEqual([r["status"] for r in results], ["unowned"])
        self.assertEqual([event[0] for event in self.events].count("notice.undeliverable"), 2)


class AdapterTests(unittest.TestCase):
    def test_a_channel_adapter_becomes_a_sender_that_posts_to_the_persons_conversation(self):
        posted: list[dict] = []

        class Fake:
            def send_reply(self, reply):
                posted.append({"conversation": reply.conversation_id, "thread": reply.thread_id, "text": reply.text})
                return "ts"

        delivery.adapter_sender(Fake())("D0123ABC", "hello")
        self.assertEqual(posted, [{"conversation": "D0123ABC", "thread": None, "text": "hello"}])

    def test_a_real_slack_adapter_posts_a_handoff_notice_to_the_persons_direct_conversation(self):
        posted: list[dict] = []
        adapter = slack.SlackChannelAdapter(b"s" * 32, bot_user_id="U0BOT0001", transport=lambda message: posted.append(message) or "1700000000.000100")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "handoffs"
            request = {"agent": "receptionist", "tool": "calendar.book", "requestId": "a" * 64, "arguments": {"summary": SECRET}}
            handoff.create(root, request, capability="calendar", reason="unbound", assignee=JORDAN, deadline_hours=24, now=T0)
            directory = delivery.Directory({JORDAN: {"channel": "slack", "address": "D0123ABC"}})
            results = delivery.deliver([delivery.handoff_source(root, now=T0)], directory, {"slack": delivery.adapter_sender(adapter)})
        self.assertEqual(results[0]["status"], "delivered")
        self.assertEqual((posted[0]["method"], posted[0]["channel"]), ("chat.postMessage", "D0123ABC"))
        self.assertNotIn("thread_ts", posted[0])
        self.assertNotIn("Acacia", json.dumps(posted))


class CliTests(Case):
    def test_the_deliver_command_delivers_to_inbox_people_and_reports_everyone_else_without_marking_them(self):
        import contextlib
        import io

        cli = _load("runtime_handoff_for_delivery", "scripts/runtime-handoff.py")
        self.create(1, JORDAN)
        self.create(2, PRIYA)
        people = pathlib.Path(self._tmp.name) / "people.json"
        people.write_text(json.dumps({JORDAN: {"channel": "slack", "address": "D1AAA"}, PRIYA: {"channel": "inbox"}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([str(self.root), "deliver", "--people", str(people)])
        self.assertEqual(code, 0)
        results = {item["to"]: item for item in json.loads(out.getvalue())}
        self.assertEqual((results[JORDAN]["status"], results[PRIYA]["status"]), ("no-channel", "delivered"))
        pending = {item["assignee"]: item for item in handoff.pending(self.root)}
        self.assertIn("assigned", pending[PRIYA]["notices"])
        self.assertNotIn("notices", pending[JORDAN])
        ledger = (self.root.parent / "audit.jsonl").read_text()
        self.assertIn("notice.delivered", ledger)
        self.assertIn("notice.undeliverable", ledger)
        self.assertNotIn("D1AAA", ledger)

    def test_a_bad_directory_fails_closed(self):
        import contextlib
        import io

        cli = _load("runtime_handoff_for_delivery2", "scripts/runtime-handoff.py")
        self.create()
        people = pathlib.Path(self._tmp.name) / "people.json"
        people.write_text(json.dumps({JORDAN: {"channel": "slack", "address": "x", "token": "y"}}))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([str(self.root), "deliver", "--people", str(people)]), 1)
        self.assertNotIn("notices", handoff.pending(self.root)[0])


if __name__ == "__main__":
    unittest.main()
