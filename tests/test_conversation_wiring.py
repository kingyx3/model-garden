"""Credential-free checks that finished calls and handled emails reach the cross-channel timeline, and that a person can run it (#115, #108, #109)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
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


worker_tests = _load("worker_cases_for_wiring", "tests/test_runtime_worker.py")
filing_tests = _load("filing_cases_for_wiring", "tests/test_conversation_filing.py")
timeline_cli = _load("runtime_conversations_under_test", "scripts/runtime-conversations.py")
operator = _load("client_operator_for_wiring", "scripts/client-operator.py")
recorder_module = _load("recorder_for_wiring", "platform/channels/conversation_recorder.py")
conversations = recorder_module._conversations()

PERSON = "jordan@acme.example"
ALEX = "alex@customer.example"
CRM_MANIFEST = {"capabilities": [
    {"family": "kb", "status": "available", "provider": "text-knowledge", "tier": 0},
    {"family": "crm", "status": "available", "provider": "built-in-crm", "tier": 0},
]}
UNVERIFIED = worker_tests.raw().replace(b"spf=pass; dkim=pass; dmarc=pass", b"spf=fail; dkim=fail; dmarc=fail")


class MailWiringTests(worker_tests.Case):
    def make_worker(self, *, crm: bool):
        runtime = self.make({"mailbox": {"addresses": [worker_tests.MAILBOX], "trustedAuthservId": worker_tests.MX, "owner": worker_tests.OWNER},
                             "people": {worker_tests.OWNER: {"channel": "slack", "address": "D0"}}},
                            coworker=worker_tests.FakeCoworker(), senders={"slack": lambda address, text: None})
        if crm:
            (self.root / "manifest.json").write_text(json.dumps(CRM_MANIFEST))
        return runtime

    def store(self, runtime):
        return conversations.ConversationStore(self.data / "conversations", tenant=runtime.tenant)

    def ingest(self, runtime, message, now):
        runtime._spool().ingest(message, now=now)
        self.clock[0] += 3600  # the worker runs each job on its own interval
        return runtime.tick()

    def only_conversation(self, runtime):
        queue = self.store(runtime)
        self.assertEqual(queue.stats()["conversations"], 1)
        state = json.loads((self.data / "conversations" / f"{runtime.tenant}.conversations.json").read_text())
        (conversation,) = state["conversations"].values()
        return conversation, state

    def test_a_handled_email_is_on_the_timeline_by_reference_and_the_content_is_nowhere_in_it(self):
        runtime = self.make_worker(crm=False)
        self.ingest(runtime, worker_tests.raw(), worker_tests.T0 - 5)
        conversation, _ = self.only_conversation(runtime)
        self.assertEqual(conversation["channel"], "email")
        self.assertEqual({item["kind"] for item in conversation["interactions"]}, {"message", "outcome"})
        text = re.sub(r"[0-9a-f]{16,}|\d{2}:\d{2}:\d{2}\.\d+Z", "", "".join(path.read_text() for path in (self.data / "conversations").iterdir() if path.suffix == ".json"))  # ids are random hex
        for private in ("4111", "opening hours", ALEX, "customer.example", "Hours"):
            self.assertNotIn(private, text)

    def test_the_same_email_twice_adds_nothing(self):
        runtime = self.make_worker(crm=False)
        self.ingest(runtime, worker_tests.raw(), worker_tests.T0 - 5)
        before = self.only_conversation(runtime)[0]["interactions"]
        runtime.mailbox_handler()(worker_tests.raw(), worker_tests.T0)
        self.assertEqual(self.only_conversation(runtime)[0]["interactions"], before)

    def test_a_verified_sender_links_to_the_matching_contact_and_an_unverified_one_does_not(self):
        runtime = self.make_worker(crm=True)
        contact = runtime._sidecar().crm_store().create_contact({"name": "Alex", "email": ALEX}, now=worker_tests.T0 - 100)
        self.ingest(runtime, UNVERIFIED, worker_tests.T0 - 5)
        self.assertEqual(self.store(runtime).stats()["linked"], 0)  # a header the server did not verify is no evidence
        self.ingest(runtime, worker_tests.raw("<c2@customer.example>"), worker_tests.T0 - 4)
        stats = self.store(runtime).stats()
        self.assertEqual(stats["linked"], 1)
        state = json.loads((self.data / "conversations" / f"{runtime.tenant}.conversations.json").read_text())
        (link,) = state["links"].values()
        self.assertEqual((link["customerId"], link["confidence"]), (contact["objectId"], "verified"))

    def test_a_contact_added_while_the_worker_runs_is_matched_by_the_next_email(self):
        runtime = self.make_worker(crm=True)
        self.ingest(runtime, worker_tests.raw(), worker_tests.T0 - 5)
        self.assertEqual(self.store(runtime).stats()["linked"], 0)
        runtime._sidecar().crm_store().create_contact({"name": "Alex", "email": ALEX}, now=worker_tests.T0)
        self.ingest(runtime, worker_tests.raw("<c9@customer.example>"), worker_tests.T0 - 4)
        self.assertEqual(self.store(runtime).stats()["conversations"], 2)  # a new thread: its own conversation, now linked
        self.assertEqual(self.store(runtime).stats()["linked"], 1)

    def test_without_a_tier_zero_contact_log_conversations_are_recorded_and_never_linked(self):
        runtime = self.make_worker(crm=False)
        self.ingest(runtime, worker_tests.raw(), worker_tests.T0 - 5)
        self.assertEqual(self.store(runtime).stats(), {"conversations": 1, "linked": 0, "review": 0})

    def test_a_timeline_failure_never_stops_the_reply(self):
        runtime = self.make_worker(crm=False)
        with mock.patch.object(conversations.ConversationStore, "open", side_effect=OSError("disk full")):
            report = self.ingest(runtime, worker_tests.raw(), worker_tests.T0 - 5)
        self.assertEqual(report["mailbox"]["done"], 1)
        self.assertEqual(len(runtime._artifacts().list()), 1)


class CallWiringTests(filing_tests.CommandTests):
    def conversations_root(self):
        return self.root / "conversations"

    def run_filing(self, *records, extra=()):
        path = self.root / "t.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        code, out, err = self.run_cli(["--transcripts", str(path), "--conversations-root", str(self.conversations_root()), *self.common(), *extra])
        return code, json.loads(out) if out.strip() else None, err

    def test_each_filed_call_is_on_the_timeline_by_reference_and_reruns_add_nothing(self):
        calls = (filing_tests.transcript("call-a", started=1_790_000_000), filing_tests.transcript("call-b", started=1_790_000_500, outcome="transferred"))
        code, out, _ = self.run_filing(*calls)
        self.assertEqual((code, out["filed"], out["timeline"]), (0, 2, {"recorded": 2, "failed": 0, "skipped": 0}))
        store = conversations.ConversationStore(self.conversations_root(), tenant="acme")
        self.assertEqual(store.stats(), {"conversations": 2, "linked": 0, "review": 0})
        first = (self.conversations_root() / "acme.conversations.json").read_text()
        code, out, _ = self.run_filing(*calls)
        self.assertEqual(out["duplicate"], 2)
        self.assertEqual((self.conversations_root() / "acme.conversations.json").read_text(), first)
        for private in (filing_tests.SECRET_WORDS, "Hargreaves"):
            self.assertNotIn(private, first)
        kinds = {item["kind"] for item in store.conversation_timeline(store.conversation_id("voice", "call-b"))}
        self.assertEqual(kinds, {"call", "outcome", "handoff", "summary"})  # the post-call summary: a digest and source label, never its words

    def test_a_timeline_problem_never_changes_how_the_call_was_filed(self):
        blocker = self.root / "conversations"
        blocker.write_text("a file where the directory should be")
        code, out, _ = self.run_filing(filing_tests.transcript("call-a"))
        self.assertEqual((code, out["filed"], out["failed"]), (0, 1, 0))
        self.assertEqual(out["timeline"]["failed"], 1)


class OperatorCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.audit = self.root / "audit.jsonl"
        self.store = conversations.ConversationStore(self.root / "c", tenant="acme", audit=lambda *event: None)
        recorder = recorder_module.ConversationRecorder(
            self.store, agent="receptionist", directory=lambda kind, digest: ["c-1"] if kind == "phone" else [])
        self.cid = recorder.record_call({"callHash": "call-a", "timestamp": 1_790_000_000, "outcome": "resolved"}, caller_number="+6591234567")["conversationId"]
        self.other = recorder.record_email("<t@x>", "<m@x>", 1_790_000_100, verified_sender="alex@x.example")["conversationId"]

    def run_cli(self, *args, now=1_790_100_000):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = timeline_cli.main([str(self.root / "c"), "--tenant", "acme", "--audit-path", str(self.audit), *args], now=now)
        return code, out.getvalue(), err.getvalue()

    def ledger(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def test_stats_review_and_timeline_show_references_and_no_content(self):
        code, out, _ = self.run_cli("stats")
        self.assertEqual((code, json.loads(out)), (0, {"conversations": 2, "linked": 0, "review": 1}))
        code, out, _ = self.run_cli("review")
        (item,) = json.loads(out)
        self.assertEqual((item["reason"], item["candidates"], item["conversationId"]), ("asserted-match", ["c-1"], self.cid))
        code, out, _ = self.run_cli("timeline", "--conversation", self.cid)
        rows = json.loads(out)
        self.assertEqual([row["kind"] for row in rows], ["call", "outcome"])
        self.assertTrue(all(row["ref"].split(":")[0] in {"transcript", "outcome"} for row in rows))
        for private in ("6591234567", "alex@x.example"):
            self.assertNotIn(private, out)

    def test_confirming_links_the_customer_and_is_ledgered_under_the_named_person(self):
        (item,) = json.loads(self.run_cli("review")[1])
        code, out, _ = self.run_cli("confirm", item["id"], "--customer", "c-1", "--by", "jordan@acme.example")
        self.assertEqual((code, json.loads(out)["customerId"]), (0, "c-1"))
        self.assertEqual(json.loads(self.run_cli("stats")[1])["review"], 0)
        rows = json.loads(self.run_cli("timeline", "--customer", "c-1")[1])
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(event.get("action") == "conversation.link" and event.get("performedBy") == "jordan@acme.example" for event in self.ledger()))

    def test_erasure_and_retention_are_ledgered_and_remove_the_timeline(self):
        self.assertEqual(self.run_cli("erase", "--conversation", self.cid, "--by", PERSON)[0], 0)
        self.assertEqual(json.loads(self.run_cli("stats")[1])["conversations"], 1)
        code, out, _ = self.run_cli("purge", "--older-than-days", "1", "--by", PERSON)
        self.assertEqual((code, json.loads(out)), (0, {"conversations": 1}))
        self.assertEqual(json.loads(self.run_cli("stats")[1]), {"conversations": 0, "linked": 0, "review": 0})
        self.assertEqual([event["action"] for event in self.ledger() if event["action"].startswith("conversation.")], ["conversation.erase", "conversation.purge"])

    def test_a_person_can_list_link_and_unlink_a_conversation_the_queue_never_held(self):
        code, out, _ = self.run_cli("list", "--unlinked")
        self.assertEqual({row["id"] for row in json.loads(out)}, {self.cid, self.other})
        self.assertEqual(self.run_cli("link", "--conversation", self.other, "--customer", "c-9", "--by", PERSON)[0], 0)
        self.assertEqual([row["id"] for row in json.loads(self.run_cli("list", "--unlinked")[1])], [self.cid])
        self.assertEqual(len(json.loads(self.run_cli("timeline", "--customer", "c-9")[1])), 1)  # the email's message
        code, _, err = self.run_cli("link", "--conversation", self.other, "--customer", "c-10", "--by", PERSON)
        self.assertEqual(code, 1)
        self.assertIn("already linked", err)
        self.assertEqual(self.run_cli("unlink", "--conversation", self.other, "--by", PERSON)[0], 0)
        self.assertEqual(json.loads(self.run_cli("timeline", "--customer", "c-9")[1]), [])
        actions = [event["action"] for event in self.ledger() if event["action"].startswith("conversation.")]
        self.assertEqual(actions, ["conversation.link", "conversation.unlink"])

    def test_bad_input_fails_cleanly(self):
        for args in (("confirm", "rv-nope", "--customer", "c-1", "--by", PERSON), ("dismiss", "rv-nope", "--by", PERSON),
                     ("timeline", "--conversation", "cv-nope"), ("erase", "--customer", "c-1", "--by", " "),
                     ("purge", "--older-than-days", "0", "--by", PERSON), ("purge", "--older-than-days", "9999", "--by", PERSON)):
            code, out, err = self.run_cli(*args)
            self.assertEqual(code, 1, args)
            self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(self.run_cli("stats")[1])["conversations"], 2)

    def test_the_tenant_defaults_to_the_compiled_client_as_the_worker_names_it(self):
        desired = self.root / "d.json"
        desired.write_text(json.dumps({"source": {"client": "acme-dental"}}))
        self.assertEqual(timeline_cli.default_tenant(desired), "acme-dental")
        with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
            timeline_cli.default_tenant(self.root / "missing.json")


class OperatorCliTests(unittest.TestCase):
    def test_the_operator_command_validates_before_touching_docker_and_passes_arguments(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0, stdout=("gov1\n" if command[:2] == ["docker", "ps"] else "{}\n"), stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), contextlib.redirect_stdout(io.StringIO()):
            operator.runtime_conversations("acme-dev", "stats")
            operator.runtime_conversations("acme-dev", "confirm", target="rv-0123456789abcdef", customer="c-1", by=PERSON)
            operator.runtime_conversations("acme-dev", "timeline", conversation="cv-0123456789abcdef0123")
            operator.runtime_conversations("acme-dev", "purge", older_than_days=365, by=PERSON)
            operator.runtime_conversations("acme-dev", "list", unlinked=True)
            operator.runtime_conversations("acme-dev", "link", conversation="cv-0123456789abcdef0123", customer="c-1", by=PERSON)
            operator.runtime_conversations("acme-dev", "unlink", conversation="cv-0123456789abcdef0123", by=PERSON)
        executed = [command for command in calls if command[:2] == ["docker", "exec"]]
        self.assertEqual(executed[0][-2:], ["/opt/data/.modelgarden/conversations", "stats"])
        self.assertEqual(executed[1][-6:], ["confirm", "rv-0123456789abcdef", "--customer", "c-1", "--by", PERSON])
        self.assertEqual(executed[2][-2:], ["--conversation", "cv-0123456789abcdef0123"])
        self.assertEqual(executed[3][-4:], ["--older-than-days", "365", "--by", PERSON])
        self.assertEqual(executed[4][-2:], ["list", "--unlinked"])
        cv = "cv-0123456789abcdef0123"
        self.assertEqual(executed[5][-7:], ["link", "--conversation", cv, "--customer", "c-1", "--by", PERSON])
        self.assertEqual(executed[6][-5:], ["unlink", "--conversation", cv, "--by", PERSON])
        before = len(calls)
        for command, kwargs in (("link", {"conversation": "cv-0123456789abcdef0123", "by": PERSON}),
                                ("link", {"customer": "c-1", "by": PERSON}),
                                ("unlink", {"by": PERSON}), ("unlink", {"conversation": "cv-0123456789abcdef0123", "customer": "c-1", "by": PERSON}),
                                ("link", {"conversation": "cv-0123456789abcdef0123", "customer": "c-1"}),
                                ("wipe", {}), ("confirm", {"target": "nope", "customer": "c-1", "by": PERSON}),
                                ("confirm", {"target": "rv-0123456789abcdef", "by": PERSON}),
                                ("confirm", {"target": "rv-0123456789abcdef", "customer": "c-1"}),
                                ("timeline", {}), ("timeline", {"conversation": "cv-0123456789abcdef0123", "customer": "c-1"}),
                                ("timeline", {"conversation": "bad"}), ("erase", {"by": PERSON}), ("erase", {"customer": "c-1"}),
                                ("purge", {"older_than_days": 0, "by": PERSON}), ("purge", {"older_than_days": 5})):
            with self.assertRaises(ValueError, msg=(command, kwargs)):
                operator.runtime_conversations("acme-dev", command, **kwargs)
        self.assertEqual(len(calls), before)


class PackagingTests(unittest.TestCase):
    def test_the_runtime_image_carries_the_modules_and_command_the_wiring_needs(self):
        source = (ROOT / "scripts" / "deploy-client-docker.py").read_text(encoding="utf-8")
        for needed in ("platform/core/conversations.py", "platform/channels/conversation_recorder.py", "scripts/runtime-conversations.py"):
            self.assertIn(f'pathlib.Path("{needed}")', source)
            self.assertTrue((ROOT / needed).is_file())


if __name__ == "__main__":
    unittest.main()
