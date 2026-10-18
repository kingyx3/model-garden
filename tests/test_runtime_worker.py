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
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = _load("runtime_worker_under_test", "platform/runtime/worker.py")
coworker_client = worker._module("coworker")
T0 = calendar.timegm((2026, 10, 19, 8, 0, 0))  # Monday 08:00 UTC
OWNER = "jordan@acme.example"
MX = "mx.acme.example"
MAILBOX = "support@acme.example"
SECRET_BODY = "What are your opening hours? My card 4111 1111 1111 1111."


def compiled(root: pathlib.Path, *, schedules=None) -> pathlib.Path:
    path = root / "desired-state.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                    "--agent", "receptionist", "--output", str(path)], check=True, capture_output=True)
    if schedules is not None:
        state = json.loads(path.read_text())
        state["schedules"] = schedules
        path.write_text(json.dumps(state))
    return path


def manifest(root: pathlib.Path) -> pathlib.Path:
    path = root / "manifest.json"
    path.write_text(json.dumps({"capabilities": [{"family": "kb", "status": "available", "provider": "text-knowledge", "tier": 0}]}))
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

    def make(self, config=None, *, schedules=None, coworker=None, senders=None):
        import os
        os.environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(manifest(self.root))
        self.addCleanup(os.environ.pop, "MODEL_GARDEN_CAPABILITY_MANIFEST", None)
        return worker.Worker(compiled(self.root, schedules=schedules), self.data, config or {}, coworker=coworker,
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
        self.assertEqual(runtime.queue.pending(), [])

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
        ledger = re.sub(r"[0-9a-f]{32,}", "", (self.data / "audit.jsonl").read_text())  # digests are random hex that can hold "4111"
        self.assertNotIn("4111", ledger)
        self.assertIn('"kb.search"', ledger)  # the knowledge read is a governed, ledgered read

    def test_mail_with_no_approved_answer_goes_to_a_person(self):
        runtime = self.make(self.config(), coworker=FakeCoworker(answer=False), senders={"slack": lambda address, text: None})
        runtime._spool().ingest(raw(), now=T0 - 5)
        runtime.tick()
        self.assertEqual(runtime._artifacts().list(), [])
        self.assertEqual(runtime.queue.pending(), [])  # delivered in the same pass
        self.assertIn("support-mailbox.handled", (self.data / "audit.jsonl").read_text())


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


if __name__ == "__main__":
    unittest.main()


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
