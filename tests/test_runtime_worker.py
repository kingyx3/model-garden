"""Credential-free checks for the runtime worker: schedules applied on deploy, runs, mailbox drain, notices and retention."""
from __future__ import annotations

import calendar
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
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


worker = _load("runtime_worker_under_test", "platform/runtime/worker.py")
outbox_module = worker._module("outbox")
coworker_client = worker._module("coworker")
T0 = calendar.timegm((2026, 10, 19, 8, 0, 0))  # Monday 08:00 UTC
OWNER = "jordan@acme.example"
MX = "mx.acme.example"
MAILBOX = "support@acme.example"
SECRET_BODY = "What are your opening hours? My card 4111 1111 1111 1111."


def compiled(root: pathlib.Path, *, schedules=None, client=None) -> pathlib.Path:
    path = root / "desired-state.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                    "--agent", "receptionist", "--output", str(path)], check=True, capture_output=True)
    if schedules is not None or client is not None:
        state = json.loads(path.read_text())
        if schedules is not None:
            state["schedules"] = schedules
        if client is not None:
            state["source"]["client"] = client
        path.write_text(json.dumps(state))
    return path


def manifest(root: pathlib.Path, *, tickets: bool = False, crm: bool = False) -> pathlib.Path:
    path = root / "manifest.json"
    entries = [{"family": "kb", "status": "available", "provider": "text-knowledge", "tier": 0}]
    if crm:
        entries.append({"family": "crm", "status": "available", "provider": "built-in-crm", "tier": 0})
    if tickets:
        entries.append({"family": "tickets", "status": "available", "provider": "built-in-tickets", "tier": 0})
    path.write_text(json.dumps({"capabilities": entries}))
    return path


SCHEDULE = {"name": "monday-summary", "owner": OWNER, "agent": "receptionist", "goal": "Summarise open enquiries for me",
            "cron": "0 9 * * 1", "budget": {"steps": 3, "tokens": 5000}, "requiredCapabilities": ["kb.search"]}


class FakeCoworker:
    def __init__(self, answer=True):
        self.calls: list[tuple] = []
        self.give_answer = answer

    def complete(self, instructions, message, *, session=None):
        self.calls.append(("complete", session))
        return {"text": "done", "usage": {"tokens": 100}}

    def classify(self, text, labels, *, purpose, session=None):
        self.calls.append(("classify", session))
        return "enquiry"

    def answer(self, question, sections, *, session=None):
        sections = list(sections)
        self.calls.append(("answer", [section["source"] for section in sections]))
        if not self.give_answer or not sections:
            return None
        return {"text": "We are open 9 to 6.", "sources": [sections[0]["source"]]}


def raw(message_id="<c1@customer.example>") -> bytes:
    return (f"Authentication-Results: {MX}; spf=pass; dkim=pass; dmarc=pass header.from=customer.example\r\n"
            f"From: Alex <alex@customer.example>\r\nTo: {MAILBOX}\r\nSubject: Hours\r\nMessage-ID: {message_id}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{SECRET_BODY}\r\n").encode()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.clock = [T0]

    def make(self, config=None, *, schedules=None, coworker=None, senders=None, tickets=False, crm=False):
        import os
        os.environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(manifest(self.root, tickets=tickets, crm=crm))
        self.addCleanup(os.environ.pop, "MODEL_GARDEN_CAPABILITY_MANIFEST", None)
        return worker.Worker(compiled(self.root, schedules=schedules, client="acme" if tickets else None), self.data, config or {}, coworker=coworker,
                             senders=senders, clock=lambda: self.clock[0])

    def events(self):
        path = self.data / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ConfigTests(Case):
    def test_unknown_settings_secrets_and_public_coworker_urls_are_refused(self):
        with self.assertRaisesRegex(worker.WorkerConfigError, "unknown"):
            worker.load_config({"apiKey": "x"})
        with self.assertRaisesRegex(coworker_client.CoworkerError, "private service"):
            worker.load_config({"coworker": {"url": "https://api.example.com/v1"}})
        with self.assertRaisesRegex(worker.WorkerConfigError, "key comes from"):
            worker.load_config({"coworker": {"url": "http://coworker-api:8642/v1", "key": "s3cret"}})
        with self.assertRaisesRegex(worker.WorkerConfigError, "intervals"):
            worker.load_config({"intervals": {"scheduler": 1}})
        with self.assertRaisesRegex(worker.WorkerConfigError, "owners"):
            worker.load_config({"owners": {OWNER: ["rm -rf"]}})


class ScheduleTests(Case):
    def test_workspace_schedules_are_applied_once_on_start_under_the_deployer(self):
        runtime = self.make({"deployedBy": "deploy:alex@acme.example"}, schedules=[SCHEDULE])
        report = runtime.tick()
        self.assertTrue(report["schedules"]["applied"])
        self.assertEqual([entry["managedBy"] for entry in runtime._schedule_store().list()], ["monday-summary"])
        self.assertNotIn("schedules", runtime.tick())
        applied = [event for event in self.events() if event.get("performedBy") == "deploy:alex@acme.example"]
        self.assertTrue(applied)
        self.assertNotIn("Summarise open enquiries", (self.data / "audit.jsonl").read_text())

    def test_no_deployer_means_nothing_is_applied(self):
        runtime = self.make({}, schedules=[SCHEDULE])
        self.assertEqual(runtime.tick()["schedules"], {"applied": False, "reason": "no-deployer-named"})

    def test_a_due_schedule_runs_through_the_coworker_within_the_owners_permissions(self):
        fake = FakeCoworker()
        runtime = self.make({"deployedBy": "deploy:alex", "owners": {OWNER: ["kb.search"]}, "people": {OWNER: {"channel": "inbox"}}},
                            schedules=[SCHEDULE], coworker=fake)
        runtime.tick()
        self.clock[0] = T0 + 3600 + 60  # past Monday 09:00
        report = runtime.tick()
        self.assertEqual(report["scheduler"]["runs"], 1)
        self.assertEqual(fake.calls[0][0], "complete")
        self.assertTrue(fake.calls[0][1].startswith("task:"))
        run = runtime._schedule_store().runs()[0]
        self.assertEqual(run["state"], "succeeded")

    def test_an_owner_without_permissions_is_refused_and_told(self):
        fake = FakeCoworker()
        runtime = self.make({"deployedBy": "deploy:alex", "people": {OWNER: {"channel": "inbox"}}}, schedules=[SCHEDULE], coworker=fake)
        runtime.tick()
        self.clock[0] = T0 + 3700
        report = runtime.tick()
        self.assertEqual(fake.calls, [])
        self.assertEqual(runtime._schedule_store().runs()[0]["reason"], "authority-refused")
        # the owner notice is queued by the scheduler and delivered to the owner's channel in the same pass
        self.assertGreaterEqual(report["notices"].get("delivered", 0), 1)
        # Inbox posting is durable, not a completed human acknowledgement.
        self.assertEqual(len(runtime.queue.pending()), 1)
        self.assertTrue(runtime.queue.pending()[0]["inboxPosted"])

    def test_without_a_coworker_nothing_runs(self):
        runtime = self.make({"deployedBy": "deploy:alex"}, schedules=[SCHEDULE])
        self.assertEqual(runtime.tick()["scheduler"], {"skipped": "no-coworker-configured"})


class MailboxTests(Case):
    def config(self):
        return {"mailbox": {"addresses": [MAILBOX], "trustedAuthservId": MX, "owner": OWNER},
                "people": {OWNER: {"channel": "slack", "address": "D0OWNER"}}}

    def test_inbound_mail_is_answered_from_approved_knowledge_as_a_draft_and_the_owner_is_told(self):
        fake, sent = FakeCoworker(), []
        runtime = self.make(self.config(), coworker=fake, senders={"slack": lambda address, text: sent.append((address, text))})
        runtime._spool().ingest(raw(), now=T0 - 5)
        report = runtime.tick()
        self.assertEqual(report["mailbox"]["done"], 1)
        kinds = [call[0] for call in fake.calls]
        self.assertEqual(kinds, ["classify", "answer"])
        self.assertTrue(fake.calls[1][1][0].startswith("knowledge:"))
        drafts = runtime._artifacts().list()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(sent[0][0], "D0OWNER")
        self.assertIn("reply draft", sent[0][1])
        ledger = re.sub(r"[0-9a-f]{16,}|\d{2}:\d{2}:\d{2}\.\d+Z", "", (self.data / "audit.jsonl").read_text())  # digests are random hex that can hold "4111"
        self.assertNotIn("4111", ledger)
        self.assertIn('"kb.search"', ledger)  # the knowledge read is a governed, ledgered read

    def test_mail_with_no_approved_answer_goes_to_a_person(self):
        runtime = self.make(self.config(), coworker=FakeCoworker(answer=False), senders={"slack": lambda address, text: None})
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        self.assertEqual(runtime._artifacts().list(), [])
        self.assertEqual(runtime.queue.pending(), [])  # delivered in the same pass
        self.assertIn("support-mailbox.handled", (self.data / "audit.jsonl").read_text())


    def test_mail_for_a_person_also_opens_one_ticket_when_the_built_in_store_is_bound(self):
        runtime = self.make(self.config(), coworker=FakeCoworker(answer=False), senders={"slack": lambda address, text: None}, tickets=True)
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        store = runtime._sidecar().tickets_store()
        (item,) = store.open_items()
        shown = json.dumps(store.get(item["ticketId"]))
        for private in ("4111", "alex@customer.example", "opening hours"):
            self.assertNotIn(private, shown)
        opened = [event for event in self.events() if event.get("action") == "ticket.opened"]
        self.assertEqual([(event["performedBy"], event["details"]["ticketId"], event["details"]["reason"]) for event in opened],
                         [("runtime:support-mailbox", item["ticketId"], "no-approved-answer")])
        runtime._spool().ingest(raw("<c2@customer.example>"), now=T0 - 4)  # a different thread is its own ticket
        self.clock[0] += 3600
        runtime.tick()
        self.assertEqual(len(store.open_items()), 2)

    def test_without_a_bound_ticket_store_mail_for_a_person_opens_nothing(self):
        runtime = self.make(self.config(), coworker=FakeCoworker(answer=False), senders={"slack": lambda address, text: None})
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        self.assertFalse((self.data / "tickets").exists())
        self.assertNotIn("ticket.opened", (self.data / "audit.jsonl").read_text())


class EventTriggerTests(Case):
    def config(self):
        return {"mailbox": {"addresses": [MAILBOX], "trustedAuthservId": MX, "owner": OWNER}, "owners": {OWNER: ["kb.search"]},
                "people": {OWNER: {"channel": "slack", "address": "D0OWNER"}}}

    def trigger(self, runtime, event="mail.received", **overrides):
        values = dict(owner=OWNER, agent="receptionist", goal="Look at what just arrived", event=event, budget={"steps": 3, "tokens": 5000},
                      required_capabilities=["kb.search"], now=T0)
        values.update(overrides)
        return runtime._schedule_store().create_trigger(**values)

    def make_runtime(self, coworker, **kwargs):
        return self.make(self.config(), coworker=coworker, senders={"slack": lambda address, text: None}, **kwargs)

    def runs(self, runtime):
        return runtime._schedule_store().runs()

    def test_a_trigger_on_mail_received_runs_once_for_each_handled_message(self):
        fake = FakeCoworker()
        runtime = self.make_runtime(fake)
        self.trigger(runtime)
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime._spool().ingest(raw("<c2@customer.example>"), now=T0 - 4)
        runtime.tick()
        runs = self.runs(runtime)
        self.assertEqual([run["trigger"] for run in runs], ["event:mail.received", "event:mail.received"])
        self.assertEqual([call[0] for call in fake.calls].count("complete"), 2)
        shown = json.dumps(self.events())
        self.assertNotIn("alex@customer.example", shown)
        self.assertNotIn("4111", re.sub(r"[0-9a-f]{16,}|\d{2}:\d{2}:\d{2}\.\d+Z", "", shown))  # only the kind and a digest of the reference reach the runner and the ledger (hex ids are random)

    def test_a_redelivered_message_is_not_a_second_event(self):
        fake = FakeCoworker()
        runtime = self.make_runtime(fake)
        self.trigger(runtime)
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        runtime._spool().ingest(raw(), now=T0 - 4)  # the same message arriving again
        self.clock[0] += 3600
        runtime.tick()
        self.assertEqual(len(self.runs(runtime)), 1)

    def test_loops_and_spam_are_not_events_and_a_trigger_for_another_event_stays_quiet(self):
        fake = FakeCoworker()
        runtime = self.make_runtime(fake)
        self.trigger(runtime, event="ticket.created")
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        self.assertEqual(self.runs(runtime), [])  # an answered enquiry opens no ticket, so nothing fired
        auto = raw("<auto@customer.example>").replace(b"Subject: Hours", b"Subject: Out of office\r\nAuto-Submitted: auto-replied")
        quiet = self.make_runtime(FakeCoworker())
        self.trigger(quiet)
        quiet._spool().ingest(auto, now=T0 - 5)
        quiet.tick()
        self.assertEqual(self.runs(quiet), [])

    def test_mail_that_needs_a_person_fires_ticket_created_as_well_as_mail_received(self):
        fake = FakeCoworker(answer=False)
        runtime = self.make_runtime(fake, tickets=True)
        self.trigger(runtime, event="ticket.created")
        self.trigger(runtime, event="mail.received", goal="Note the new mail")
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        self.assertEqual(sorted(run["trigger"] for run in self.runs(runtime)), ["event:mail.received", "event:ticket.created"])
        self.assertEqual(len(runtime._sidecar().tickets_store().open_items()), 1)

    def test_a_trigger_at_its_hourly_limit_is_skipped_and_a_failing_runner_never_loses_the_mail(self):
        fake = FakeCoworker()
        runtime = self.make_runtime(fake)
        self.trigger(runtime, max_per_hour=1)
        for index in range(3):
            runtime._spool().ingest(raw(f"<c{index}@customer.example>"), now=T0 - 5 + index)
        runtime.tick()
        self.assertEqual(len(self.runs(runtime)), 1)

        class Broken(FakeCoworker):
            def complete(self, *args, **kwargs):
                raise RuntimeError("customer text must not leak")

        failing = self.make_runtime(Broken())
        self.trigger(failing)
        failing._spool().ingest(raw("<f1@customer.example>"), now=T0 - 5)
        report = failing.tick()
        self.assertEqual(report["mailbox"]["done"], 1)  # the message was still handled and filed
        self.assertNotIn("customer text must not leak", (self.data / "audit.jsonl").read_text())

    def test_without_a_coworker_events_are_not_run(self):
        runtime = self.make(self.config(), senders={"slack": lambda address, text: None})
        self.assertEqual(runtime.fire_event("mail.received", "ref", T0), {"skipped": "no-coworker-configured"})


class EventSourceTests(Case):
    """The sources the worker does not witness itself: chat messages the gateway reported and tickets opened by any route (#99, #77)."""

    config, trigger, make_runtime, runs = (EventTriggerTests.config, EventTriggerTests.trigger, EventTriggerTests.make_runtime, EventTriggerTests.runs)

    def outbox(self):
        return outbox_module.EventOutbox(self.data / "events")

    def event_runs(self, runtime, kind):
        return [run for run in self.runs(runtime) if run["trigger"] == f"event:{kind}"]

    def test_each_chat_message_the_gateway_reported_runs_a_chat_trigger_once_and_only_a_digest_reaches_the_runner(self):
        fake = FakeCoworker()
        runtime = self.make_runtime(fake)
        self.trigger(runtime, event="chat.message", goal="Note the new message")
        ref = "ab" * 16
        self.outbox().append("chat.message", ref, at=T0)
        self.outbox().append("chat.message", ref, at=T0)  # the same event reported twice
        self.outbox().append("chat.message", "cd" * 16, at=T0)
        report = runtime.tick()
        self.assertEqual(report["events"]["chat"], 3)
        self.assertEqual(len(self.event_runs(runtime, "chat.message")), 2)  # once per distinct event
        self.clock[0] += 60
        self.assertEqual(runtime.tick()["events"]["chat"], 0)  # consumed: nothing runs again
        self.assertEqual(len(self.event_runs(runtime, "chat.message")), 2)
        shown = json.dumps(self.events())
        self.assertNotIn(ref, shown)  # the ledger and the runner see a digest of the digest, never the reference

    def test_a_trigger_for_another_event_stays_quiet_on_chat_and_nothing_is_lost_without_a_coworker(self):
        runtime = self.make(self.config(), coworker=None, senders={"slack": lambda address, text: None})
        self.outbox().append("chat.message", "ab" * 16, at=T0)
        self.assertEqual(runtime.tick()["events"], {"skipped": "no-coworker-configured"})
        self.assertEqual(len(self.outbox().read(0)[0]), 1)  # left for when a coworker is configured
        quiet = self.make_runtime(FakeCoworker())
        self.trigger(quiet, event="mail.received")
        self.outbox().append("chat.message", "cd" * 16, at=T0)
        quiet.tick()
        self.assertEqual(self.runs(quiet), [])

    def test_a_ticket_opened_by_any_route_after_the_worker_started_runs_a_ticket_trigger_once(self):
        runtime = self.make_runtime(FakeCoworker(), tickets=True)
        store = runtime._sidecar().tickets_store()
        store.create(project="support", title="Before the worker watched", requested_by="runtime:old", now=T0 - 500)
        self.trigger(runtime, event="ticket.created", goal="Look at the new ticket")
        self.assertEqual(runtime.tick()["events"], {"chat": 0, "tickets": 0})  # first pass starts now: history is not replayed
        self.clock[0] += 60
        made = store.create(project="support", title="Boiler is leaking, card 4111 1111 1111 1111", requested_by="alex@customer.example", now=int(self.clock[0]) - 5)
        report = runtime.tick()
        self.assertEqual(report["events"]["tickets"], 1)
        self.assertEqual(len(self.event_runs(runtime, "ticket.created")), 1)
        self.clock[0] += 60
        self.assertEqual(runtime.tick()["events"]["tickets"], 0)  # seen: not run again
        self.assertEqual(len(self.event_runs(runtime, "ticket.created")), 1)
        self.assertNotIn("Boiler", json.dumps(self.events()))
        self.assertNotIn(made["ticketId"], json.dumps(self.events()))

    def test_a_ticket_the_mailbox_already_announced_is_not_run_a_second_time_by_the_poll(self):
        runtime = self.make_runtime(FakeCoworker(answer=False), tickets=True)
        self.trigger(runtime, event="ticket.created", goal="Look at the new ticket")
        runtime.tick()  # the poll starts
        self.clock[0] += 60
        runtime._spool().ingest(raw(), now=int(self.clock[0]) - 5)
        runtime.tick()  # the mailbox opens a ticket and announces it; the poll sees it in the same pass
        self.clock[0] += 60
        runtime.tick()
        self.assertEqual(len(self.event_runs(runtime, "ticket.created")), 1)

    def test_tickets_created_in_the_same_second_are_each_run_once_across_passes(self):
        runtime = self.make_runtime(FakeCoworker(), tickets=True)
        store = runtime._sidecar().tickets_store()
        self.trigger(runtime, event="ticket.created", goal="Look at the new ticket", max_per_hour=20)
        runtime.tick()
        for number in (1, 2):
            self.clock[0] += 31
            store.create(project="support", title=f"Ticket {number}", requested_by="a@x.example", now=int(self.clock[0]))
            runtime.tick()
        self.clock[0] += 31
        store.create(project="support", title="Ticket 3", requested_by="a@x.example", now=int(self.clock[0]) - 31)  # same second as ticket 2
        runtime.tick()
        self.assertEqual(len(self.event_runs(runtime, "ticket.created")), 3)

    def test_without_a_ticket_store_only_chat_events_are_handled(self):
        runtime = self.make_runtime(FakeCoworker())
        self.assertEqual(runtime.tick()["events"], {"chat": 0, "tickets": 0})
        self.assertFalse((self.data / "tickets").exists())


class KnowledgeReviewTests(Case):
    """An approved source due for review, or past its reviewBy date, is announced once to the contact (declared dates only) (#116, #111, #77)."""

    def runtime(self, sources, *, config=None):
        sent = []
        runtime = self.make(config if config is not None else {"escalateTo": OWNER, "people": {OWNER: {"channel": "slack", "address": "D0OWNER"}}},
                            senders={"slack": lambda address, text: sent.append(text)})
        base = runtime.desired_state["knowledge"][0]
        runtime.desired_state["knowledge"] = [
            {**base, "metadata": {**base["metadata"], "name": name},
             "spec": {"type": "text-file", "status": "approved", "effectiveDate": "2026-01-01", "audiences": ["receptionist"], **spec}}
            for name, spec in sources.items()
        ]
        self.sent = sent
        return runtime

    def told(self):
        return [text for text in self.sent if "business-rules source" in text]

    def test_a_source_due_within_two_weeks_and_one_already_stale_are_each_announced_once(self):
        runtime = self.runtime({"hours": {"reviewBy": "2026-10-25"}, "prices": {"reviewBy": "2026-10-10"},
                                "far-future": {"reviewBy": "2027-06-01"}, "no-review-date": {}})
        runtime.tick()
        self.clock[0] += 60
        runtime.tick()  # notices are delivered on the pass after the job queued them
        told = self.told()
        self.assertEqual(len(told), 2)
        self.assertTrue(any("hours" in text and "due for review on 2026-10-25" in text for text in told))
        self.assertTrue(any("prices" in text and "stopped answering" in text for text in told))
        self.assertFalse(any("far-future" in text or "no-review-date" in text for text in told))
        self.clock[0] += 86_400
        runtime.last_run.clear()
        runtime.tick()
        runtime.tick()
        self.assertEqual(len(self.told()), 2)  # not repeated while the condition lasts

    def test_the_same_source_is_announced_again_when_it_moves_from_due_soon_to_stale(self):
        runtime = self.runtime({"hours": {"reviewBy": "2026-10-25"}})
        runtime.tick()
        self.clock[0] += 60
        runtime.tick()
        self.assertEqual(len(self.told()), 1)
        self.clock[0] += 8 * 86_400  # past 2026-10-25
        runtime.last_run.clear()
        runtime.tick()
        self.clock[0] += 60
        runtime.tick()
        told = self.told()
        self.assertEqual(len(told), 2)
        self.assertIn("stopped answering", told[1])

    def test_a_draft_a_source_for_another_agent_an_invalid_date_and_a_non_text_source_are_not_announced(self):
        runtime = self.runtime({"drafty": {"reviewBy": "2026-10-25", "status": "draft"}, "elsewhere": {"reviewBy": "2026-10-25", "audiences": ["sales"]},
                                "garbled": {"reviewBy": "next week"}, "repo": {"reviewBy": "2026-10-25", "type": "repository"}})
        report = runtime.run_knowledge(T0)
        self.assertEqual(report, {"dueSoon": 0, "stale": 0})

    def test_with_nobody_to_tell_it_says_so_and_remembers_nothing(self):
        runtime = self.runtime({"hours": {"reviewBy": "2026-10-25"}}, config={})
        self.assertEqual(runtime.run_knowledge(T0), {"skipped": "nobody-to-tell"})
        self.assertFalse((self.data / "worker" / "told.json").exists())

    def test_only_the_source_name_and_date_reach_the_notice(self):
        runtime = self.runtime({"hours": {"reviewBy": "2026-10-10"}})
        runtime.run_knowledge(T0)
        (notice,) = [item for item in runtime.queue.pending() if item["kind"] == "knowledge-review"]
        self.assertEqual(notice["to"], OWNER)
        self.assertNotIn("Hargreaves", notice["text"])


class CrmFilingTests(Case):
    """A handled customer email is filed to the built-in contact log by the worker, retried after an outage and never reported as filed unless acknowledged."""

    def config(self):
        return {"mailbox": {"addresses": [MAILBOX], "trustedAuthservId": MX, "owner": OWNER}, "owners": {OWNER: ["kb.search"]},
                "people": {OWNER: {"channel": "slack", "address": "D0OWNER"}}}

    def runtime(self, crm=True):
        self.sent: list[tuple[str, str]] = []
        return self.make(self.config(), coworker=FakeCoworker(), senders={"slack": lambda address, text: self.sent.append((address, text))}, crm=crm)

    def entries(self, runtime):
        return runtime._crm_filing()[0].list()

    def test_a_handled_email_is_filed_through_the_governed_boundary_and_ledgered(self):
        runtime = self.runtime()
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        (entry,) = self.entries(runtime)
        self.assertEqual((entry["channel"], entry["state"], entry["attempts"]), ("email", "synced", 1))
        self.assertTrue(entry["providerRef"].startswith("built-in-crm:interaction/"))
        executed = [event for event in self.events() if event.get("event") == "execution" and event.get("tool") == "crm.log-interaction"]
        self.assertEqual([event["outcome"] for event in executed], ["succeeded"])
        self.assertEqual(executed[0]["ledger"]["externalObjectRef"], entry["providerRef"])
        self.assertEqual(runtime._crm_filing()[1].keys(), [])  # acknowledged, so nothing is kept for a retry

    def interactions(self, runtime):
        with runtime._sidecar().crm_store()._locked() as state:
            return [dict(item) for item in state["interactions"].values()]

    def test_a_verified_sender_is_filed_against_the_one_contact_holding_the_address(self):
        runtime = self.runtime()
        made = runtime._sidecar().crm_store().create_contact({"name": "Alex Customer", "email": "Alex@Customer.example"})
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        (interaction,) = self.interactions(runtime)
        self.assertEqual(interaction["contact"], made["objectId"])
        self.assertEqual(runtime._sidecar().crm_store().review_queue()["unmatchedInteractions"], [])

    def test_no_holder_or_several_holders_stay_in_the_unmatched_queue_for_a_person(self):
        runtime = self.runtime()
        runtime._spool().ingest(raw(), now=T0 - 5)  # nobody holds the address
        runtime.tick()
        store = runtime._sidecar().crm_store()
        store.create_contact({"name": "Alex One", "email": "alex@customer.example"})
        store.create_contact({"name": "Alex Two", "email": "alex@customer.example"})  # a possible duplicate: ambiguous, so not guessed
        runtime._spool().ingest(raw("<c2@customer.example>"), now=T0 - 4)
        self.clock[0] += 60  # past the mailbox job's interval
        runtime.tick()
        self.assertEqual([item["contact"] for item in self.interactions(runtime)], ["unmatched", "unmatched"])

    def test_an_unverified_sender_is_never_matched_to_a_contact(self):
        runtime = self.runtime()
        runtime._sidecar().crm_store().create_contact({"name": "Alex Customer", "email": "alex@customer.example"})
        forged = raw().replace(b"spf=pass; dkim=pass; dmarc=pass", b"spf=fail; dkim=fail; dmarc=fail")
        runtime._spool().ingest(forged, now=T0 - 5)
        runtime.tick()
        self.assertEqual([item["contact"] for item in self.interactions(runtime)], ["unmatched"])

    def test_filing_is_off_without_the_built_in_contact_log_and_the_job_says_so(self):
        runtime = self.runtime(crm=False)
        runtime._spool().ingest(raw(), now=T0 - 5)
        report = runtime.tick()
        self.assertEqual(report["crm-sync"], {"skipped": "no-built-in-crm-bound"})
        self.assertFalse((self.data / "crm-sync").exists())

    def test_a_redelivered_message_is_filed_once(self):
        runtime = self.runtime()
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        runtime._spool().ingest(raw(), now=T0 - 4)
        self.clock[0] += 3600
        runtime.tick()
        self.assertEqual(len(self.entries(runtime)), 1)
        self.assertEqual(len([event for event in self.events() if event.get("tool") == "crm.log-interaction" and event.get("event") == "execution"]), 1)

    def test_an_outage_is_retried_after_its_backoff_and_then_filed_without_a_second_record(self):
        runtime = self.runtime()
        filing = runtime._crm_filing()
        real = runtime._crm_filing
        calls = []

        def flaky():
            ledger, store, executor = real()

            def maybe(arguments):
                calls.append(1)
                if len(calls) == 1:
                    raise TimeoutError("crm unreachable")
                return executor(arguments)

            return ledger, store, maybe

        runtime._crm_filing = flaky
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        (entry,) = self.entries(runtime)
        self.assertEqual((entry["state"], entry["lastError"]), ("failed", "outage"))
        self.assertEqual(len(filing[1].keys()), 1)  # kept for the retry
        self.clock[0] += 5  # still inside the backoff
        runtime.last_run.clear()
        self.assertEqual(runtime.run_crm_sync(int(self.clock[0]))["attempted"], 0)
        self.clock[0] += 120
        outcome = runtime.run_crm_sync(int(self.clock[0]))
        self.assertEqual((outcome["attempted"], outcome["synced"]), (1, 1))
        (entry,) = self.entries(runtime)
        self.assertEqual(entry["state"], "synced")
        self.assertEqual(filing[1].keys(), [])
        self.assertEqual(len([event for event in self.events() if event.get("tool") == "crm.log-interaction" and event.get("event") == "execution"]), 1)

    def test_a_filing_that_needs_approval_blocks_for_a_person_who_is_told_once(self):
        runtime = self.runtime()
        real = runtime._crm_filing

        def pending():
            ledger, store, _ = real()

            def refuse(arguments):
                raise PermissionError("filing was not allowed to run (pending-approval)")

            return ledger, store, refuse

        runtime._crm_filing = pending
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        (entry,) = self.entries(runtime)
        self.assertEqual((entry["state"], entry["lastError"]), ("blocked", "unauthorized"))
        told = [text for address, text in self.sent if "could not be filed to the CRM" in text]  # the same pass's crm-sync job told the owner
        self.assertEqual(len(told), 1)
        self.assertNotIn("alex@customer.example", told[0])
        self.assertIn("crm-sync list", told[0])
        again = runtime.run_crm_sync(T0 + 2)
        self.assertEqual((again["blocked"], again["notified"], again["attempted"]), (1, 0, 0))  # already told, and a blocked entry is not retried

    def test_what_is_kept_and_ledgered_holds_no_message_text(self):
        runtime = self.runtime()
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        stored = "".join(path.read_text() for path in (self.data / "crm-sync").rglob("*.json"))
        text = re.sub(r"[0-9a-f]{16,}|\d{2}:\d{2}:\d{2}\.\d+Z", "", stored + (self.data / "audit.jsonl").read_text())  # digests and ids are random hex that can contain "4111"
        for private in ("4111", "opening hours", "alex@customer.example"):
            self.assertNotIn(private, text)


def call_record(call_hash="a" * 64, start=T0 - 600, end=T0 - 540):
    return {"type": "CallTranscript", "callHash": call_hash, "timestamp": start, "endedAt": end, "outcome": "completed",
            "turns": [{"index": 0, "speaker": "caller", "text": "Hi, what are your hours?", "offsetSeconds": 0},
                      {"index": 1, "speaker": "agent", "text": "We are open 9 to 6.", "offsetSeconds": 4}]}


class CallFilingTests(Case):
    """Finished phone calls are read from the voice transcript volume and filed by the worker, with a summary and ledger request ids."""

    def runtime(self, records, *, crm=True, coworker=None, extra_lines=()):
        self.transcripts = self.root / "voice" / "transcripts.jsonl"
        self.transcripts.parent.mkdir()
        self.transcripts.write_text("".join(json.dumps(item) + "\n" for item in [*records, *extra_lines]), encoding="utf-8")
        runtime = self.make(crm=crm, coworker=coworker)
        runtime.voice_transcripts = self.transcripts
        return runtime

    def interactions(self, runtime):
        with runtime._sidecar().crm_store()._locked() as state:
            return [dict(item) for item in state["interactions"].values()]

    def test_emergency_pause_holds_transcript_filing_and_model_summary_until_resume(self):
        coworker = FakeCoworker()
        runtime = self.runtime([call_record()], coworker=coworker)
        store = worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "synthetic incident", T0)
        self.assertEqual(runtime.run_calls(T0), {"skipped": "paused"})
        self.assertEqual(coworker.calls, [], "paused worker must not summarize customer calls")
        self.assertEqual(self.interactions(runtime), [], "paused worker must not file a transcript")
        store.resume(OWNER, T0 + 1)
        self.assertEqual(runtime.run_calls(T0 + 2)["filed"], 1)

    def test_inflight_transcript_filing_cannot_race_a_confirmed_owner_pause(self):
        runtime = self.runtime([call_record()])
        store = worker._module("pause").PauseStore(self.data)
        filing = worker._module("filing")
        original = filing.read_call_transcripts
        entered, release = threading.Event(), threading.Event()
        reports, failures = [], []

        def blocked_read(path):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("synthetic test filing was not released")
            return original(path)

        def run_filing():
            try:
                reports.append(runtime.run_calls(T0))
            except BaseException as exc:
                failures.append(type(exc).__name__)

        thread = threading.Thread(target=run_filing, daemon=True)
        with mock.patch.object(filing, "read_call_transcripts", side_effect=blocked_read):
            thread.start()
            try:
                self.assertTrue(entered.wait(3), "filing must enter its guarded work")
                with self.assertRaisesRegex(TimeoutError, "not acknowledged"):
                    store.pause(OWNER, "synthetic incident", T0, lock_timeout_seconds=0.05)
                self.assertFalse(store.is_paused(), "timeout is not a successful pause")
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(reports[0]["filed"], 1)
        store.pause(OWNER, "synthetic incident", T0 + 1, lock_timeout_seconds=1)
        self.assertEqual(runtime.run_calls(T0 + 2), {"skipped": "paused"})

    def test_a_call_is_filed_unmatched_with_a_model_summary_once(self):
        coworker = FakeCoworker()
        runtime = self.runtime([call_record()], coworker=coworker)
        report = runtime.tick()["calls"]
        self.assertEqual((report["filed"], report["notFiled"]), (1, 0))
        self.assertEqual(report["summaries"]["model"], 1)
        (interaction,) = self.interactions(runtime)
        self.assertEqual((interaction["contact"], interaction["summary"], interaction["channel"]), ("unmatched", "done", "voice"))
        executed = [event for event in self.events() if event.get("tool") == "crm.log-interaction" and event.get("event") == "execution"]
        self.assertEqual({event["initiatingUser"] for event in executed}, {"runtime:conversation-filing"})
        self.clock[0] += 120
        again = runtime.tick()["calls"]
        self.assertEqual((again["filed"], again["duplicate"]), (0, 1))
        self.assertEqual([call for call in coworker.calls if call[0] == "complete"], [("complete", "call-summary:" + "a" * 64)])

    def test_without_a_coworker_the_fallback_summary_is_filed(self):
        runtime = self.runtime([call_record()])
        report = runtime.tick()["calls"]
        self.assertEqual((report["filed"], report["summaries"]["fallback"]), (1, 1))
        self.assertIn("Outcome: completed", self.interactions(runtime)[0]["summary"])

    def test_the_job_is_off_without_the_volume_or_a_bound_contact_log(self):
        self.assertEqual(self.make().tick()["calls"], {"skipped": "no-voice-transcripts"})
        runtime = self.runtime([call_record()], crm=False)
        self.assertEqual(runtime.tick()["calls"], {"skipped": "no-crm-bound"})

    def test_a_transcript_write_failure_is_ledgered_once(self):
        failure = {"type": "TranscriptWriteFailed", "timestamp": T0 - 30, "roomHash": "b" * 64, "errorType": "OSError"}
        runtime = self.runtime([], extra_lines=[failure])
        self.assertEqual(runtime.tick()["calls"]["writeFailures"], {"ledgered": 1, "known": 0})
        self.clock[0] += 120
        self.assertEqual(runtime.tick()["calls"]["writeFailures"], {"ledgered": 0, "known": 1})

    def test_request_ids_from_the_ledger_reach_the_conversation_timeline(self):
        import datetime as dt
        runtime = self.runtime([])
        sidecar = runtime._sidecar("runtime:hermes")
        sidecar.crm_create(fields={"name": "Pat Caller"}, justification="during the call")
        authorized = [event for event in self.events() if event.get("event") == "authorization" and event.get("initiatingUser") == "runtime:hermes"]
        self.assertTrue(authorized)
        at = int(dt.datetime.fromisoformat(authorized[0]["timestamp"].replace("Z", "+00:00")).timestamp())
        runtime.voice_transcripts.write_text(json.dumps(call_record(start=at - 20, end=at + 20)) + "\n", encoding="utf-8")
        runtime.tick()
        store = runtime._conversation_recorder()._store
        timeline = store.conversation_timeline(store.conversation_id("voice", "a" * 64))
        self.assertEqual([item["requestId"] for item in timeline if item["kind"] == "action"], [authorized[0]["requestId"]])

    def test_the_transcript_path_comes_from_the_environment(self):
        args = type("Args", (), {"config": None, "desired_state": compiled(self.root), "data_root": self.data})()
        self.assertEqual(worker.build(args, {"MODEL_GARDEN_VOICE_TRANSCRIPTS": "/opt/voice/transcripts.jsonl"}).voice_transcripts,
                         pathlib.Path("/opt/voice/transcripts.jsonl"))
        self.assertIsNone(worker.build(args, {}).voice_transcripts)


class LoopTests(Case):
    def test_a_failing_job_is_ledgered_by_type_and_does_not_stop_the_others(self):
        runtime = self.make({})
        runtime.run_scheduler = lambda now: (_ for _ in ()).throw(RuntimeError("customer text must not leak"))
        report = runtime.tick()
        self.assertEqual(report["scheduler"], {"failed": "RuntimeError"})
        self.assertIn("notices", report)
        failed = [event for event in self.events() if event.get("action") == "worker.job-failed"]
        self.assertTrue(failed)
        self.assertNotIn("customer text", (self.data / "audit.jsonl").read_text())
        heartbeat = json.loads((self.data / "worker" / "heartbeat.json").read_text())
        self.assertEqual(heartbeat["failed"], ["scheduler"])
        self.assertTrue(worker.healthy(self.data / "worker" / "heartbeat.json", now=T0 + 10))
        self.assertFalse(worker.healthy(self.data / "worker" / "heartbeat.json", now=T0 + 1000))

    def test_retention_removes_expired_approvals_and_ledgers_each_by_id_only(self):
        runtime = self.make({})
        approvals = self.data / "approvals"
        old, fresh = "ab" * 32, "cd" * 32
        for kind, key in (("pending", "requestedAt"), ("decisions", "decidedAt")):
            (approvals / kind).mkdir(parents=True)
            for request_id, stamp in ((old, T0 - 30 * 3600), (fresh, T0 - 3600)):
                (approvals / kind / f"{request_id}.json").write_text(json.dumps({"requestId": request_id, key: stamp, "arguments": {"secret": "customer text"}}))
        report = runtime.tick()
        self.assertEqual(report["retention"]["approvals"], 2)
        self.assertFalse((approvals / "pending" / f"{old}.json").exists())
        self.assertFalse((approvals / "decisions" / f"{old}.json").exists())
        self.assertTrue((approvals / "pending" / f"{fresh}.json").exists())
        expired = [event for event in self.events() if event.get("action") == "approval.expired"]
        self.assertEqual(sorted(event["details"]["kind"] for event in expired), ["decision", "request"])
        self.assertTrue(all(event["details"]["requestId"] == old for event in expired))
        self.assertNotIn("customer text", (self.data / "audit.jsonl").read_text())

    def test_an_approval_that_expires_undecided_tells_the_contact_once_by_tool_name_only(self):
        sent = []
        runtime = self.make({"escalateTo": OWNER, "people": {OWNER: {"channel": "slack", "address": "D0OWNER"}}},
                            senders={"slack": lambda address, text: sent.append((address, text))})
        approvals = self.data / "approvals"
        old, decided = "ab" * 32, "cd" * 32
        (approvals / "pending").mkdir(parents=True)
        (approvals / "decisions").mkdir(parents=True)
        (approvals / "pending" / f"{old}.json").write_text(json.dumps({
            "requestId": old, "tool": "crm.update", "requestedAt": T0 - 30 * 3600, "arguments": {"note": "customer text"}, "justification": "private reason"}))
        (approvals / "decisions" / f"{decided}.json").write_text(json.dumps({"requestId": decided, "approved": True, "decidedAt": T0 - 30 * 3600}))
        runtime.tick()  # retention runs after notices in a pass, so the notice goes out on the next one
        self.clock[0] += 60
        runtime.tick()
        told = [text for address, text in sent if "expired" in text]
        self.assertEqual(len(told), 1)  # the stale decision is dropped quietly: nothing was waiting
        self.assertIn("crm.update", told[0])
        self.assertIn(old[:12], told[0])
        for private in ("customer text", "private reason"):
            self.assertNotIn(private, told[0])
        self.clock[0] += 7200
        runtime.tick()
        self.assertEqual(len([text for address, text in sent if "expired" in text]), 1)  # not repeated

    def test_an_expired_request_with_nobody_to_tell_is_still_removed_and_ledgered(self):
        runtime = self.make({})
        pending = self.data / "approvals" / "pending"
        pending.mkdir(parents=True)
        (pending / f"{'ab' * 32}.json").write_text(json.dumps({"requestId": "ab" * 32, "tool": "BAD TOOL!", "requestedAt": T0 - 30 * 3600}))
        runtime.tick()
        self.assertEqual(list(pending.glob("*.json")), [])
        self.assertEqual(len([event for event in self.events() if event.get("action") == "approval.expired"]), 1)
        self.assertEqual(runtime.queue.pending(), [])

    def test_jobs_respect_their_interval(self):
        runtime = self.make({})
        self.assertIn("retention", runtime.tick())
        self.clock[0] += 60
        report = runtime.tick()
        self.assertNotIn("retention", report)
        self.assertIn("notices", report)

    def test_a_person_without_a_channel_keeps_the_notice_due(self):
        runtime = self.make({})
        runtime.queue.add("schedule-run-failed", OWNER, "Your scheduled task did not complete.", reference="s-1:run-1")
        report = runtime.tick()
        self.assertEqual(report["notices"], {"no-channel": 1})
        self.assertEqual(len(runtime.queue.pending()), 1)


class StartupFailureTests(Case):
    def run_main(self, config, env):
        import contextlib
        import io
        import os
        from unittest import mock
        path = self.root / "worker.json"
        path.write_text(json.dumps(config))
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stderr(err):
            code = worker.main(["--desired-state", str(compiled(self.root)), "--data-root", str(self.data),
                                "--config", str(path), "--once"])
        return code, err.getvalue()

    def test_short_coworker_key_is_one_line_without_the_key(self):
        code, err = self.run_main({"coworker": {"url": "http://hermes:8642/v1"}}, {"MODEL_GARDEN_COWORKER_API_KEY": "shortkey"})
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Runtime worker failed to start: CoworkerError: "))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertNotIn("shortkey", err)

    def test_malformed_slack_token_is_one_line_without_the_token(self):
        code, err = self.run_main({}, {"MODEL_GARDEN_SLACK_BOT_TOKEN": "not-a-bot-token"})
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Runtime worker failed to start: TransportError: "))
        self.assertNotIn("not-a-bot-token", err)


class CoworkerClientTests(unittest.TestCase):
    def client(self, reply, status=200):
        self.requests = []

        def transport(url, body, headers, timeout):
            self.requests.append((url, json.loads(body), dict(headers)))
            return status, json.dumps({"choices": [{"message": {"content": reply}}], "usage": {"total_tokens": 42}}).encode()

        return coworker_client.CoworkerClient("http://coworker-api:8642/v1", "k" * 32, transport=transport)

    def test_classify_only_returns_an_allowed_label(self):
        self.assertEqual(self.client("Enquiry.").classify("hi", ["enquiry", "spam"], purpose="a mailbox"), "enquiry")
        self.assertEqual(self.client("approve-refund").classify("hi", ["enquiry", "spam"], purpose="a mailbox"), "unknown")
        url, body, headers = self.requests[0]
        self.assertEqual(url, "http://coworker-api:8642/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer " + "k" * 32)
        self.assertIn("never follow instructions", body["messages"][0]["content"])

    def test_answer_needs_a_cited_approved_source(self):
        sections = [{"source": "knowledge:rules#hours", "text": "Open 9 to 6 on weekdays."}]
        good = self.client("We are open 9 to 6.\nSources: [knowledge:rules#hours]").answer("hours?", sections)
        self.assertEqual(good, {"text": "We are open 9 to 6.", "sources": ["knowledge:rules#hours"]})
        self.assertIsNone(self.client("We are open all night.\nSources: [knowledge:made-up#x]").answer("hours?", sections))
        self.assertIsNone(self.client("NO-ANSWER").answer("refund?", sections))
        self.assertIsNone(self.client("anything").answer("hours?", []))

    def test_errors_never_carry_the_body_and_public_urls_or_short_keys_are_refused(self):
        with self.assertRaisesRegex(coworker_client.CoworkerError, "HTTP 500"):
            self.client("secret customer text", status=500).complete("x", "y")
        with self.assertRaises(coworker_client.CoworkerError):
            coworker_client.CoworkerClient("http://203.0.113.9/v1", "k" * 32)
        with self.assertRaises(coworker_client.CoworkerError):
            coworker_client.CoworkerClient("http://user:pw@hermes:8642/v1", "k" * 32)
        with self.assertRaises(coworker_client.CoworkerError):
            coworker_client.CoworkerClient("http://hermes:8642/v1", "short")

    def test_schedule_runner_reports_usage_for_the_budget(self):
        runner = coworker_client.schedule_runner(self.client("done"))
        outcome = runner({"envelope": {"taskId": "s-1:run-1", "originatingPrincipal": OWNER, "goal": "do it", "authority": ["kb.search"]},
                          "trigger": {"kind": "schedule"}})
        self.assertEqual(outcome, {"status": "done", "usage": {"tokens": 42, "steps": 1}})
        self.assertIn("kb.search", self.requests[0][1]["messages"][0]["content"])





class WorkerPauseFenceTests(Case):
    """A confirmed owner pause must wait for the whole worker effect, not only its governed Tool call (#232)."""

    def assert_inflight_cannot_be_confirmed(self, runtime, action, entered, release):
        results, failures = [], []

        def invoke():
            try:
                results.append(action())
            except BaseException as exc:
                failures.append(type(exc).__name__)

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(3), "worker must enter the material operation")
            with self.assertRaisesRegex(TimeoutError, "not acknowledged"):
                worker._module("pause").PauseStore(self.data).pause(
                    OWNER, "synthetic safety incident", T0, lock_timeout_seconds=0.05,
                )
            self.assertFalse(worker._module("pause").PauseStore(self.data).is_paused(),
                             "a timeout must not claim a completed owner pause")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)
        return results[0]

    def test_scheduled_model_run_is_inflight_until_coworker_finishes(self):
        entered, release = threading.Event(), threading.Event()

        class Blocking(FakeCoworker):
            def complete(self, *args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test scheduler was not released")
                return super().complete(*args, **kwargs)

        fake = Blocking()
        runtime = self.make({"deployedBy": "deploy:alex", "owners": {OWNER: ["kb.search"]}},
                            schedules=[SCHEDULE], coworker=fake)
        runtime.apply_schedules(T0)
        outcome = self.assert_inflight_cannot_be_confirmed(runtime, lambda: runtime.run_scheduler(T0 + 3700), entered, release)
        self.assertEqual(outcome["runs"], 1)
        store = worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "synthetic safety incident", T0 + 1)
        self.assertEqual(runtime.run_scheduler(T0 + 3800), {"skipped": "paused"})
        self.assertEqual(len(fake.calls), 1)

    def test_mail_classification_and_draft_cannot_race_a_confirmed_pause(self):
        entered, release = threading.Event(), threading.Event()

        class Blocking(FakeCoworker):
            def classify(self, *args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test mailbox was not released")
                return super().classify(*args, **kwargs)

        fake = Blocking()
        runtime = self.make({"mailbox": {"addresses": [MAILBOX], "trustedAuthservId": MX, "owner": OWNER}},
                            coworker=fake)
        runtime._spool().ingest(raw(), now=T0 - 5)
        outcome = self.assert_inflight_cannot_be_confirmed(runtime, lambda: runtime.run_mailbox(T0), entered, release)
        self.assertEqual(outcome["done"], 1)
        store = worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "synthetic safety incident", T0 + 1)
        runtime._spool().ingest(raw("<paused@customer.example>"), now=T0 + 2)
        self.assertEqual(runtime.run_mailbox(T0 + 3), {"skipped": "paused"})
        self.assertEqual(len([call for call in fake.calls if call[0] == "classify"]), 1)
        store.resume(OWNER, T0 + 4)
        self.assertEqual(runtime.run_mailbox(T0 + 5)["done"], 1)

    def test_event_trigger_does_not_consume_events_or_run_across_pause(self):
        entered, release = threading.Event(), threading.Event()

        class Blocking(FakeCoworker):
            def complete(self, *args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test trigger was not released")
                return super().complete(*args, **kwargs)

        fake = Blocking()
        runtime = self.make({"owners": {OWNER: ["kb.search"]}}, coworker=fake)
        runtime._schedule_store().create_trigger(
            owner=OWNER, agent="receptionist", goal="Review the event", event="chat.message",
            budget={"steps": 3, "tokens": 5000}, required_capabilities=["kb.search"], now=T0,
        )
        outbox = outbox_module.EventOutbox(self.data / "events")
        outbox.append("chat.message", "aa" * 16, at=T0)
        outcome = self.assert_inflight_cannot_be_confirmed(runtime, lambda: runtime.run_events(T0), entered, release)
        self.assertEqual(outcome["chat"], 1)
        store = worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "synthetic safety incident", T0 + 1)
        outbox.append("chat.message", "bb" * 16, at=T0 + 2)
        self.assertEqual(runtime.run_events(T0 + 3), {"skipped": "paused"})
        self.assertEqual(len(fake.calls), 1)
        store.resume(OWNER, T0 + 4)
        self.assertEqual(runtime.run_events(T0 + 5)["chat"], 1)
        self.assertEqual(len(fake.calls), 2)

    def test_crm_retry_is_fenced_even_before_the_governed_tool(self):
        entered, release = threading.Event(), threading.Event()
        runtime = self.make(crm=True)
        filing = worker._module("crm_filing")

        def blocked_retry(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test CRM retry was not released")
            return {"attempted": 0}

        with mock.patch.object(filing, "retry_due", side_effect=blocked_retry):
            outcome = self.assert_inflight_cannot_be_confirmed(runtime, lambda: runtime.run_crm_sync(T0), entered, release)
        self.assertEqual(outcome["attempted"], 0)
        store = worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "synthetic safety incident", T0 + 1)
        self.assertEqual(runtime.run_crm_sync(T0 + 2), {"skipped": "paused"})

    def test_direct_event_firing_is_also_blocked_while_paused(self):
        fake = FakeCoworker()
        runtime = self.make({"owners": {OWNER: ["kb.search"]}}, coworker=fake)
        runtime._schedule_store().create_trigger(
            owner=OWNER, agent="receptionist", goal="Review the event", event="chat.message",
            budget={"steps": 3, "tokens": 5000}, required_capabilities=["kb.search"], now=T0,
        )
        store = worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "synthetic safety incident", T0)
        self.assertEqual(runtime.fire_event("chat.message", "aa" * 16, T0 + 1), {"skipped": "paused"})
        self.assertEqual(fake.calls, [])
        store.resume(OWNER, T0 + 2)
        self.assertEqual(runtime.fire_event("chat.message", "aa" * 16, T0 + 3)["runs"], 1)



class ExportJobTests(Case):
    def test_the_export_job_copies_the_ledger_and_is_skipped_unless_configured(self):
        self.assertEqual(self.make().run_export(T0), {"skipped": "no-export-configured"})
        sink = worker._module("ledger").DirectorySink(self.root / "sink")
        configured = self.make({"export": {"gcsBucket": "acme-ledger-export"}})
        configured.export_sink = sink
        self.assertEqual(configured.run_export(T0), {"skipped": "no-ledger-yet"})
        configured.audit("worker.test", "runtime:worker", {"n": 1})
        result = configured.run_export(T0)
        self.assertGreaterEqual(result["exported"], 1)
        self.assertEqual(len(sink.records()), result["totalChainedEvents"])
        self.assertIn("export", configured.tick())

    def test_the_export_settings_are_validated_without_secrets(self):
        for bad in ({"gcsBucket": "UPPER"}, {"gcsBucket": "ok-bucket", "key": "x"}, {"impersonate": "x@y.example"}):
            with self.assertRaises((worker.WorkerConfigError, ValueError)):
                worker.load_config({"export": bad})
        good = worker.load_config({"export": {"gcsBucket": "ok-bucket", "impersonate": "mg-exp-1234abcd@acme-proj.iam.gserviceaccount.com"}})
        self.assertEqual(good["export"]["gcsBucket"], "ok-bucket")


class ExportAlertTests(Case):
    """A stalled or broken export reaches the escalation contact through the notice channel, once, then a recovery (#80)."""

    def make_exporting(self, sink):
        sent: list[tuple[str, str]] = []
        runtime = self.make({"export": {"gcsBucket": "acme-ledger-export"}, "escalateTo": OWNER,
                             "people": {OWNER: {"channel": "slack", "address": "D0OWNER"}}},
                            senders={"slack": lambda address, text: sent.append((address, text))})
        runtime.export_sink = sink
        runtime.audit("worker.test", "runtime:worker", {"n": 1})
        return runtime, sent

    def test_a_healthy_export_raises_no_alert(self):
        runtime, sent = self.make_exporting(worker._module("ledger").DirectorySink(self.root / "sink"))
        self.assertGreaterEqual(runtime.run_export(T0)["exported"], 1)
        runtime.deliver_notices(T0)
        self.assertEqual(sent, [])

    def test_an_unreachable_sink_alerts_failure_and_stale_once_then_reports_recovery(self):
        good = worker._module("ledger").DirectorySink(self.root / "sink")

        class Down:
            def put_records(self, records):
                raise worker._module("ledger").LedgerExportError("sink unreachable")

            put_head = put_records

        runtime, sent = self.make_exporting(Down())
        self.assertEqual(runtime._guard("export", runtime.run_export, T0), {"failed": "LedgerExportError"})
        runtime.deliver_notices(T0)
        self.assertEqual(len(sent), 2)  # immediate failure plus separately stale export
        self.assertTrue(all(address == "D0OWNER" for address, _ in sent))
        self.assertTrue(any("Ledger export failure problem" in text for _, text in sent))
        self.assertTrue(any("Ledger export stale problem" in text for _, text in sent))
        runtime.deliver_notices(T0 + 60)
        self.assertEqual(len(sent), 2)  # each condition delivered once, not every pass
        runtime.export_sink = good
        runtime.run_export(T0 + 120)
        runtime.deliver_notices(T0 + 120)
        self.assertEqual(len(sent), 3)
        self.assertIn("healthy again", sent[2][1])
        self.assertNotIn("4111", json.dumps(sent))

    def test_a_ledger_shorter_than_the_export_is_an_integrity_alert(self):
        runtime, sent = self.make_exporting(worker._module("ledger").DirectorySink(self.root / "sink"))
        runtime.run_export(T0)
        runtime.audit_path.write_text("", encoding="utf-8")
        self.assertEqual(runtime._guard("export", runtime.run_export, T0 + 60), {"failed": "LedgerTruncated"})
        runtime.deliver_notices(T0 + 60)
        self.assertEqual(len(sent), 1)
        self.assertIn("cannot be trusted", sent[0][1])


class UsageJobTests(Case):
    """Model usage from the Hermes session stores is ledgered as counts, once per change, for the billing sync."""

    def store(self, name="state.db"):
        import sqlite3
        path = self.root / name
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, "
                           "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, api_call_count INTEGER DEFAULT 0)")
        self.addCleanup(connection.close)
        return path, connection

    def test_usage_is_ledgered_once_per_change_and_skipped_without_sources(self):
        self.assertEqual(self.make().run_usage(T0), {"skipped": "no-usage-sources"})
        path, hermes = self.store()
        hermes.execute("INSERT INTO sessions VALUES ('s1', 'slack', 'gpt-5.1', 'Card 4111 1111 1111 1111', 900, 100, 2)")
        hermes.commit()
        runtime = self.make()
        runtime.usage_sources = {"conversations": path, "coworker": self.root / "not-started" / "state.db"}
        self.assertEqual(runtime.run_usage(T0), {"conversations": {"recorded": 1}, "coworker": {"skipped": "no-store-yet"}})
        self.assertEqual(runtime.run_usage(T0 + 300)["conversations"], {"recorded": 0})
        recorded = [event for event in self.events() if event.get("action") == "usage.recorded"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["performedBy"], "runtime:worker")
        self.assertEqual(recorded[0]["details"]["usage"], {"tokens": 1000, "costCents": 0})
        self.assertNotIn("4111 1111", json.dumps(recorded))
        timeline = worker._load("model_garden_worker_audit_query", "platform/core/audit_query.py")
        self.assertEqual(timeline._usage(recorded[0]), {"tokens": 1000, "costCents": 0})

    def test_an_unreadable_store_fails_the_job_after_metering_the_others(self):
        path, hermes = self.store()
        hermes.execute("INSERT INTO sessions VALUES ('s1', 'slack', 'gpt-5.1', NULL, 10, 1, 1)")
        hermes.commit()
        broken = self.root / "broken.db"
        broken.write_text("not a database", encoding="utf-8")
        runtime = self.make()
        runtime.usage_sources = {"conversations": path, "coworker": broken}
        self.assertEqual(runtime._guard("usage", runtime.run_usage, T0), {"failed": "UsageError"})
        self.assertEqual(len([event for event in self.events() if event.get("action") == "usage.recorded"]), 1)
        failed = [event for event in self.events() if event.get("action") == "worker.job-failed"]
        self.assertEqual(failed[-1]["details"], {"job": "usage", "error": "UsageError"})

    def test_the_sources_come_from_the_environment(self):
        args = type("Args", (), {"config": None, "desired_state": compiled(self.root), "data_root": self.data})()
        built = worker.build(args, {"MODEL_GARDEN_USAGE_SOURCES": "conversations=/opt/hermes/conversations/state.db"})
        self.assertEqual(built.usage_sources, {"conversations": pathlib.Path("/opt/hermes/conversations/state.db")})


if __name__ == "__main__":
    unittest.main()
