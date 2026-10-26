"""Credential-free checks for the chat `inbox` command: what is waiting, for the mapped person asking, in a direct conversation only (#77, #84, #88)."""
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


ingress = _load("ingress_for_inbox_command", "platform/channels/ingress.py")
inbox = _load("inbox_for_inbox_command", "platform/core/inbox.py")
handoff = _load("handoff_for_inbox_command", "platform/core/handoff.py")
tickets = _load("tickets_for_inbox_command", "platform/core/tickets.py")
approval = _load("approval_for_inbox_command", "scripts/runtime-approval.py")
text = ingress.TEXT
NOW = 1_800_000_000
SECRET = "Hargreaves boiler 12 Acacia Road"
DIRECTORY = {"slack": {"T01/UAPPROVER": {"identity": "alice@acme.example", "roles": ["approver"]},
                       "T01/UMEMBER": {"identity": "bob@acme.example"}}}


class FakeAdapter:
    def __init__(self):
        self.sent: list = []
        self.next: text.InboundMessage | None = None

    def parse_inbound(self, body, headers):
        return self.next

    def send_reply(self, reply):
        self.sent.append(reply)
        return "ts"


def request_id(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = pathlib.Path(self._tmp.name)
        self.adapter = FakeAdapter()
        self.asked: list = []
        self.events: list = []
        self.counter = 0
        self.ingress = ingress.ChannelIngress(
            {"slack": self.adapter}, text.PrincipalDirectory(DIRECTORY), respond=self.respond, decide=self.decide,
            audit=lambda action, who, details: self.events.append((action, who, details)), inbox=self.inbox_text,
        )

    def decide(self, request_id, *, approved, approver):
        return approval.decide(self.data / "approvals", request_id, approved=approved, approver=approver)

    def respond(self, inbound, principal):
        self.asked.append(inbound.text)
        return "the coworker answered"

    def inbox_text(self, inbound, principal):
        snapshot = inbox.collect(approvals=self.data / "approvals", handoffs=self.data / "handoffs", tickets=self.data / "tickets", now=NOW)
        return inbox.format_chat(inbox.for_person(snapshot, principal.identity, everything="approver" in principal.roles))

    def say(self, words, *, user="UAPPROVER", conversation="D0DIRECT01"):
        self.counter += 1
        self.adapter.next = text.InboundMessage("slack", conversation, None, f"m{self.counter}", text.ChannelIdentity("slack", user, "T01"), words)
        self.ingress.message("slack", b"{}", {})
        return self.adapter.sent[-1].text

    def waiting(self):
        for_alice = handoff.create(self.data / "handoffs", {"requestId": request_id("alice"), "tool": "calendar.book", "arguments": {"note": SECRET}},
                                   capability="calendar", reason="unbound", assignee="alice@acme.example", deadline_hours=48, now=NOW - 60)
        for_bob = handoff.create(self.data / "handoffs", {"requestId": request_id("bob"), "tool": "mail.send", "arguments": {"note": SECRET}},
                                 capability="mail", reason="human-only", assignee="bob@acme.example", deadline_hours=1, now=NOW - 7200)
        pending = self.data / "approvals" / "pending"
        pending.mkdir(parents=True)
        (pending / f"{request_id('approval')}.json").write_text(json.dumps({"requestId": request_id("approval"), "tool": "crm.create", "requestedAt": NOW - 300, "arguments": {"fields": {"name": SECRET}}}))
        store = tickets.TicketStore(self.data / "tickets", tenant="acme")
        store.create(project="support", title=SECRET, description=SECRET, requested_by="unmapped:slack:x", now=NOW - 600)
        return for_alice["id"], for_bob["id"]


class InboxCommandTests(Case):
    def test_verified_direct_approver_can_decide_exact_pending_action_in_chat(self):
        self.waiting()
        request = request_id("approval")
        reply = self.say(f"approve {request}")
        self.assertIn("Recorded: approved by you", reply)
        decision = json.loads((self.data / "approvals" / "decisions" / f"{request}.json").read_text())
        self.assertEqual((decision["requestId"], decision["approved"], decision["approver"]),
                         (request, True, "alice@acme.example"))
        self.assertNotIn(SECRET, reply)
        self.assertEqual(self.asked, [])
        self.assertIn(("channel.approval-decided", "alice@acme.example",
                       {"channel": "slack", "request": request[:16], "approved": True}), self.events)

        other = request_id("reject")
        (self.data / "approvals" / "pending" / f"{other}.json").write_text(json.dumps({
            "requestId": other, "tool": "mail.send", "requestedAt": NOW,
            "arguments": {"message": SECRET},
        }))
        self.assertIn("Recorded: rejected by you", self.say(f"reject {other}"))
        rejected = json.loads((self.data / "approvals" / "decisions" / f"{other}.json").read_text())
        self.assertEqual((rejected["approved"], rejected["approver"]), (False, "alice@acme.example"))

    def test_chat_decisions_deny_unmapped_member_and_group_without_disclosing_request(self):
        self.waiting()
        request = request_id("approval")
        for user, conversation in (("UMEMBER", "D0DIRECT01"), ("USTRANGER", "D0DIRECT01"),
                                   ("UAPPROVER", "C0TEAM001")):
            reply = self.say(f"approve {request}", user=user, conversation=conversation)
            self.assertEqual(reply, "Only a mapped approver can decide a request in a direct message.")
            self.assertNotIn(request, reply)
        self.assertFalse((self.data / "approvals" / "decisions" / f"{request}.json").exists())
        self.assertEqual(self.asked, [])

    def test_chat_decisions_require_complete_id_and_existing_unexpired_request(self):
        self.waiting()
        request = request_id("approval")
        for invalid in (request[:12], "not-a-request", request.upper()):
            self.assertIn("full 64-character", self.say(f"approve {invalid}"))
        self.assertFalse((self.data / "approvals" / "decisions" / f"{request}.json").exists())
        self.assertEqual(self.say(f"approve {request_id('missing')}"),
                         "That request is not waiting for a decision. Nothing was changed.")
        expired = request_id("expired")
        (self.data / "approvals" / "pending" / f"{expired}.json").write_text(json.dumps({
            "requestId": expired, "tool": "crm.create", "requestedAt": 1,
        }))
        self.assertEqual(self.say(f"reject {expired}"),
                         "That request is not waiting for a decision. Nothing was changed.")
        self.assertFalse((self.data / "approvals" / "decisions" / f"{expired}.json").exists())
        self.assertEqual(self.asked, [])

    def test_inbox_exposes_full_decision_reference_only_to_authorized_approver(self):
        self.waiting()
        request = request_id("approval")
        owner = self.say("inbox")
        self.assertIn(f"approve {request}", owner)
        self.assertIn(f"reject {request}", owner)
        self.assertNotIn(SECRET, owner)
        member = self.say("inbox", user="UMEMBER")
        self.assertNotIn(request, member)
        self.assertNotIn("approve ", member)

    def test_an_approver_sees_everything_with_the_reply_that_closes_each_hand_off(self):
        alice, bob = self.waiting()
        reply = self.say("inbox")
        self.assertEqual(self.asked, [])  # answered by the command, not by the coworker
        self.assertIn("Waiting: 1 approval(s), 2 hand-off(s), 1 open ticket(s), 1 overdue.", reply)
        self.assertIn("Use the Approve or Reject buttons", reply)
        self.assertIn("Hand-off for alice@acme.example: calendar.book, open", reply)
        self.assertIn(f"Reply `done {alice[:12]}`", reply)
        self.assertIn("Hand-off for bob@acme.example: mail.send, OVERDUE", reply)
        self.assertIn(f"`declined {bob[:12]} <reason>`", reply)
        self.assertIn("1 open ticket(s)", reply)
        self.assertNotIn("client-operator.py", reply)  # chat form: no shell commands
        self.assertNotIn("scripts/", reply)

    def test_nothing_the_actions_or_tickets_hold_is_ever_shown(self):
        self.waiting()
        for words in ("inbox", "/inbox", "INBOX  "):
            self.assertNotIn("Hargreaves", self.say(words))
            self.assertNotIn("Acacia", self.adapter.sent[-1].text)

    def test_a_member_sees_only_the_hand_offs_assigned_to_them(self):
        alice, bob = self.waiting()
        reply = self.say("inbox", user="UMEMBER")
        self.assertIn(f"done {bob[:12]}", reply)
        self.assertNotIn(alice[:12], reply)
        self.assertTrue(reply.startswith("Waiting: 1 hand-off(s), 1 overdue."))
        for hidden in ("approval", "ticket", "Approve"):
            self.assertNotIn(hidden, reply)
        self.assertEqual(self.say("inbox", user="UMEMBER").count("Hand-off for"), 1)

    def test_a_stranger_or_a_shared_channel_gets_the_same_neutral_words_and_nothing_else(self):
        self.waiting()
        neutral = "I can show what is waiting only to a team member in a direct message."
        self.assertEqual(self.say("inbox", user="USTRANGER"), neutral)
        self.assertEqual(self.say("inbox", user="UAPPROVER", conversation="C0TEAM001"), neutral)
        self.assertEqual(self.asked, [])

    def test_an_empty_inbox_says_so_and_only_the_exact_command_counts(self):
        self.assertEqual(self.say("inbox"), "Nothing is waiting for you.")
        self.assertEqual(self.say("what is in my inbox please"), "the coworker answered")  # ordinary words are for the coworker
        self.assertEqual(self.asked, ["what is in my inbox please"])

    def test_viewing_is_ledgered_by_person_and_channel_only(self):
        self.waiting()
        self.say("inbox")
        self.say("inbox", user="USTRANGER")
        self.assertEqual(self.events, [("channel.inbox-viewed", "alice@acme.example", {"channel": "slack"})])

    def test_without_the_inbox_hook_the_word_goes_to_the_coworker_as_before(self):
        plain = ingress.ChannelIngress({"slack": self.adapter}, text.PrincipalDirectory(DIRECTORY), respond=self.respond, decide=lambda *a, **k: {})
        self.adapter.next = text.InboundMessage("slack", "D0DIRECT01", None, "m99", text.ChannelIdentity("slack", "UAPPROVER", "T01"), "inbox")
        plain.message("slack", b"{}", {})
        self.assertEqual(self.asked, ["inbox"])


if __name__ == "__main__":
    unittest.main()
