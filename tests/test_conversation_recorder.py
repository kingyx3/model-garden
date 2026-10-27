"""Credential-free checks for putting finished calls and handled emails on the cross-channel conversation timeline (#115, #108, #109)."""
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


recorder_module = _load("conversation_recorder_under_test", "platform/channels/conversation_recorder.py")
conversations = recorder_module._conversations()
contacts = _load("contacts_for_recorder", "platform/core/contacts.py")
filing = _load("conversation_filing_for_recorder", "platform/channels/conversation_filing.py")
support = _load("support_mailbox_for_recorder", "platform/channels/support_mailbox.py")
sync = support._filing()._sync()
email_channel = support._email()
T0 = 1_800_000_000
SECRET_WORDS = "my boiler at 12 Acacia Road leaks"
REQUEST = "a" * 64


def call(call_hash="call-1", *, started=T0, outcome="message-taken", summary=None, summary_source=None, request_ids=()):
    record = {"type": "CallTranscript", "schemaVersion": 1, "timestamp": started, "endedAt": started + 95, "callHash": call_hash, "agent": "receptionist",
              "outcome": outcome, "requestIds": list(request_ids), "redactions": {},
              "turns": [{"index": 0, "speaker": "caller", "offsetSeconds": 0.0, "text": SECRET_WORDS}, {"index": 1, "speaker": "agent", "offsetSeconds": 2.0, "text": "Noted."}]}
    if summary:
        record["summary"] = summary
    if summary_source:
        record["summarySource"] = summary_source
    return record


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.events: list[tuple] = []
        self.store = conversations.ConversationStore(self.root / "conversations", tenant="acme-prod", audit=lambda *event: self.events.append(event))
        self.contacts = contacts.ContactStore(self.root / "contacts")
        self.alex = self.contacts.create_contact({"name": "Alex Tan", "email": "alex@customer.example", "phone": "+65 6123 4567"}, now=T0)["objectId"]

    def recorder(self, directory=True):
        return recorder_module.ConversationRecorder(self.store, agent="receptionist", directory=recorder_module.contacts_directory(self.contacts) if directory else None)


class DirectoryTests(Case):
    def test_the_contact_log_answers_with_the_contacts_that_hold_a_value_as_recorded(self):
        directory = recorder_module.contacts_directory(self.contacts)
        self.assertEqual(directory("email", conversations.evidence_digest("email", " ALEX@customer.example ")), [self.alex])
        self.assertEqual(directory("phone", conversations.evidence_digest("phone", "0065-6123-4567")), [self.alex])
        self.assertEqual(directory("email", conversations.evidence_digest("email", "other@customer.example")), [])
        self.assertEqual(directory("sso", "0" * 64), [])
        self.assertEqual(sorted(kind for _, kind, _ in self.contacts.identities()), ["email", "phone"])

    def test_two_contacts_holding_the_same_address_both_answer_so_the_store_sends_it_to_review(self):
        other = self.contacts.create_contact({"name": "Alex T", "email": "alex@customer.example"}, now=T0 + 1)["objectId"]
        directory = recorder_module.contacts_directory(self.contacts)
        self.assertEqual(sorted(directory("email", conversations.evidence_digest("email", "alex@customer.example"))), sorted([self.alex, other]))


class CallTests(Case):
    def test_a_call_lands_on_the_timeline_by_reference_with_its_outcome_and_never_its_words(self):
        result = self.recorder().record_call(call())
        self.assertEqual((result["state"], result["link"]), ("recorded", "unlinked"))
        rows = self.store.conversation_timeline(result["conversationId"])
        self.assertEqual([(row["kind"], row["ref"]) for row in rows], [("call", "transcript:call-1"), ("outcome", "outcome:call-1")])
        self.assertEqual(rows[1]["label"], "message-taken")
        text = self.store.path.read_text() + json.dumps(self.events)
        for secret in (SECRET_WORDS, "Acacia"):
            self.assertNotIn(secret, text)

    def test_a_transfer_adds_a_handoff_and_request_ids_become_action_receipts(self):
        result = self.recorder().record_call(call(outcome="transferred", request_ids=[REQUEST, "not-an-id"]))
        kinds = [row["kind"] for row in self.store.conversation_timeline(result["conversationId"])]
        self.assertEqual(sorted(kinds), ["action", "call", "handoff", "outcome"])
        action = next(row for row in self.store.conversation_timeline(result["conversationId"]) if row["kind"] == "action")
        self.assertEqual(action["requestId"], REQUEST)

    def test_only_a_model_summary_is_recorded_and_only_as_a_digest(self):
        provided = self.recorder().record_call(call("c-model", summary="Caller wants a callback Tuesday.", summary_source="provided"))
        rows = self.store.conversation_timeline(provided["conversationId"])
        summary = next(row for row in rows if row["kind"] == "summary")
        self.assertEqual(len(summary["contentDigest"]), 64)
        self.assertNotIn("callback", self.store.path.read_text())
        extractive = self.recorder().record_call(call("c-extractive", summary='The caller first said: "my boiler"...', summary_source="extractive"))
        self.assertNotIn("summary", [row["kind"] for row in self.store.conversation_timeline(extractive["conversationId"])])

    def test_a_caller_number_is_asserted_so_a_call_never_links_by_itself_and_goes_to_review_when_it_matches(self):
        result = self.recorder().record_call(call(), caller_number="+65 6123 4567")
        self.assertEqual(result["link"], "review")
        (item,) = self.store.review_queue()
        self.assertEqual((item["reason"], item["candidates"]), ("asserted-match", [self.alex]))
        self.assertEqual(self.store.customer_timeline(self.alex), [])
        self.assertNotIn("6123", self.store.path.read_text())

    def test_recording_the_same_call_twice_adds_nothing(self):
        recorder = self.recorder()
        first = recorder.record_call(call(summary="s", summary_source="provided", request_ids=[REQUEST]))
        again = recorder.record_call(call(summary="s", summary_source="provided", request_ids=[REQUEST]))
        self.assertEqual(first["conversationId"], again["conversationId"])
        self.assertEqual(len(self.store.conversation_timeline(first["conversationId"])), 4)

    def test_records_without_a_call_hash_or_time_are_skipped(self):
        self.assertEqual(self.recorder().record_call({"outcome": "resolved"}), {"state": "skipped"})
        self.assertEqual(self.recorder().record_call({"callHash": "x", "timestamp": "late"}), {"state": "skipped"})
        self.assertEqual(self.store.stats()["conversations"], 0)

    def test_without_a_directory_nothing_is_linked(self):
        self.assertEqual(self.recorder(directory=False).record_call(call(), caller_number="+65 6123 4567")["link"], "unlinked")


class EmailTests(Case):
    def test_a_verified_sender_links_the_thread_to_their_contact_and_the_timeline_spans_both_channels(self):
        recorder = self.recorder()
        voice = recorder.record_call(call())
        mail = recorder.record_email("<thread-1@customer.example>", "<m1@customer.example>", T0 + 3600, verified_sender="alex@customer.example", outcome="reply-drafted")
        self.assertEqual(mail["link"], "linked")
        self.assertEqual(self.store.link_of(mail["conversationId"])["customerId"], self.alex)
        review_id = None
        phone = recorder.record_call(call("call-2", started=T0 + 7200), caller_number="+65 6123 4567")
        (item,) = self.store.review_queue()
        self.store.confirm(item["id"], self.alex, by="owner@client.example", now=T0 + 8000)  # a person confirms the asserted phone match
        timeline = self.store.customer_timeline(self.alex)
        self.assertEqual({row["channel"] for row in timeline}, {"email", "voice"})
        self.assertEqual([row["at"] for row in timeline], sorted(row["at"] for row in timeline))
        self.assertNotIn(voice["conversationId"], {row["conversationId"] for row in timeline})  # the first call had no evidence: still unlinked
        self.assertIsNone(review_id)
        self.assertEqual(phone["state"], "recorded")

    def test_an_unverified_sender_adds_no_evidence_and_links_nothing(self):
        result = self.recorder().record_email("t-2", "m-2", T0, verified_sender=None, outcome="needs-person", handed_to_person=True)
        self.assertEqual(result["link"], "unlinked")
        kinds = [row["kind"] for row in self.store.conversation_timeline(result["conversationId"])]
        self.assertEqual(sorted(kinds), ["handoff", "message", "outcome"])

    def test_a_verified_address_held_by_two_contacts_goes_to_review_not_to_the_first(self):
        self.contacts.create_contact({"name": "Alex T", "email": "alex@customer.example"}, now=T0 + 1)
        result = self.recorder().record_email("t-3", "m-3", T0, verified_sender="alex@customer.example", outcome="reply-drafted")
        self.assertEqual(result["link"], "review")  # one address, two holders: no single customer is named, so a person decides
        self.assertIsNone(self.store.link_of(result["conversationId"]))
        (item,) = self.store.review_queue()
        self.assertEqual((item["reason"], len(item["candidates"])), ("conflicting-evidence", 2))

    def test_a_redelivered_message_and_a_second_message_in_the_thread_share_one_conversation(self):
        recorder = self.recorder()
        first = recorder.record_email("t-4", "m-4a", T0, verified_sender="alex@customer.example", outcome="reply-drafted")
        again = recorder.record_email("t-4", "m-4a", T0, verified_sender="alex@customer.example", outcome="reply-drafted")
        second = recorder.record_email("t-4", "m-4b", T0 + 60, verified_sender="alex@customer.example", outcome="reply-drafted")
        self.assertEqual({first["conversationId"], again["conversationId"], second["conversationId"]}, {first["conversationId"]})
        self.assertEqual(len([row for row in self.store.conversation_timeline(first["conversationId"]) if row["kind"] == "message"]), 2)

    def test_message_ids_never_reach_the_store_and_no_address_does_either(self):
        self.recorder().record_email("<thread-secret@customer.example>", "<message-secret@customer.example>", T0, verified_sender="alex@customer.example")
        text = self.store.path.read_text() + json.dumps(self.events)
        for secret in ("thread-secret", "message-secret", "customer.example", "alex@"):
            self.assertNotIn(secret, text)


class WiringTests(Case):
    def test_call_filing_with_a_recorder_also_records_the_timeline_and_never_changes_the_filing_result(self):
        class Runtime:
            def crm_log_interaction(self, **kwargs):
                return {"status": "executed", "result": {"externalObjectRef": "built-in-crm:interaction/i-1", "duplicate": False}}

        totals = filing.file_call_transcripts(Runtime(), self.contacts, [call(), call("call-2", started=T0 + 5)], recorder=self.recorder())
        self.assertEqual((totals["filed"], totals["failed"]), (2, 0))
        self.assertEqual(self.store.stats()["conversations"], 2)

    def test_a_broken_recorder_is_logged_by_type_and_does_not_stop_filing(self):
        class Runtime:
            def crm_log_interaction(self, **kwargs):
                return {"status": "executed", "result": {"externalObjectRef": "built-in-crm:interaction/i-1", "duplicate": False}}

        class Broken:
            def record_call(self, record):
                raise OSError("disk full: " + SECRET_WORDS)

        logs: list[str] = []
        totals = filing.file_call_transcripts(Runtime(), self.contacts, [call(), call("call-2", started=T0 + 5)], log=logs.append, recorder=Broken())
        self.assertEqual(totals["filed"], 2)
        self.assertEqual(len(logs), 2)
        self.assertTrue(all("OSError" in line and SECRET_WORDS not in line for line in logs))

    def test_filing_without_a_recorder_is_unchanged(self):
        class Runtime:
            def crm_log_interaction(self, **kwargs):
                return {"status": "executed", "result": {"externalObjectRef": "built-in-crm:interaction/i-1", "duplicate": False}}

        self.assertEqual(filing.file_call_transcripts(Runtime(), self.contacts, [call()])["filed"], 1)
        self.assertEqual(self.store.stats()["conversations"], 0)


MX, MAILBOX = "mx.acme.example", "support@acme.example"


def raw(*, sender="Alex Tan <alex@customer.example>", message_id="<c1@customer.example>", subject="Opening hours", body="What are your opening hours?", auth=True) -> bytes:
    header = f"Authentication-Results: {MX}; spf=pass; dkim=pass; dmarc=pass header.from=customer.example\r\n" if auth else ""
    return (f"{header}From: {sender}\r\nTo: {MAILBOX}\r\nSubject: {subject}\r\nMessage-ID: {message_id}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode("utf-8")


class MailboxWiringTests(Case):
    def mailbox(self, recorder, label="enquiry"):
        adapter = email_channel.EmailChannelAdapter(MX, own_addresses=(MAILBOX,), reply_limiter=email_channel.ReplyLimiter(self.root / "limits", 3))
        drafts: list[dict] = []

        def draft(arguments):
            drafts.append(arguments)
            return {"draftId": f"d{len(drafts)}"}

        return support.SupportMailbox(adapter, email_channel.TEXT.PrincipalDirectory(), {"mail.draft": draft}, classify=lambda message: label,
                                      answer=lambda message, category: {"text": "We are open 9 to 6.", "sources": ["kb:hours"]}, handled=support.MessageLog(self.root / "handled"),
                                      conversations=recorder, ticket=lambda _item: {"ticketId": "t-fixture"})

    def test_a_handled_enquiry_from_a_verified_sender_is_recorded_and_linked(self):
        result = self.mailbox(self.recorder()).handle(raw(), now=T0)
        self.assertEqual((result["status"], result["conversation"]), ("drafted", "recorded"))
        stats = self.store.stats()
        self.assertEqual((stats["conversations"], stats["linked"]), (1, 1))
        self.assertEqual(self.store.customer_timeline(self.alex)[0]["kind"], "message")

    def test_a_complaint_is_recorded_as_a_handoff_and_an_unverified_sender_is_not_linked(self):
        result = self.mailbox(self.recorder(), label="complaint").handle(raw(auth=False), now=T0)
        self.assertEqual((result["status"], result["conversation"]), ("needs-person", "recorded"))
        self.assertEqual(self.store.stats()["linked"], 0)
        timeline = self.store.customer_timeline(self.alex)
        self.assertEqual(timeline, [])

    def test_a_recording_failure_never_stops_the_reply_or_loses_the_message(self):
        class Broken:
            def record_email(self, *args, **kwargs):
                raise RuntimeError("store unavailable")

        mailbox = self.mailbox(Broken())
        result = mailbox.handle(raw(), now=T0)
        self.assertEqual((result["status"], result["conversation"]), ("drafted", "unavailable:RuntimeError"))
        self.assertEqual(mailbox.handle(raw(), now=T0 + 1)["reason"], "already-handled")  # still marked handled

    def test_ignored_messages_are_not_recorded(self):
        mailbox = self.mailbox(self.recorder(), label="spam")
        result = mailbox.handle(raw(), now=T0)
        self.assertEqual(result["status"], "ignored")
        self.assertNotIn("conversation", result)
        self.assertEqual(self.store.stats()["conversations"], 0)

    def test_the_mailbox_without_a_recorder_is_unchanged(self):
        result = self.mailbox(None).handle(raw(), now=T0)
        self.assertEqual(result["status"], "drafted")
        self.assertNotIn("conversation", result)


if __name__ == "__main__":
    unittest.main()
