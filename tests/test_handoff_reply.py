"""Credential-free checks for answering a hand-off or human task step from the named person's own channel."""
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


reply = _load("handoff_reply_under_test", "platform/core/handoff_reply.py")
delivery = _load("notice_delivery_for_reply", "platform/core/notice_delivery.py")
handoff = _load("handoff_for_reply", "platform/core/handoff.py")
govern = _load("govern_action_for_reply", "scripts/govern-action.py")
human_step = _load("human_step_for_reply", "platform/core/human_step.py")
T0 = 1_790_000_000
JORDAN, PRIYA, OWNER = "jordan@client.example", "priya@client.example", "owner@client.example"
PEOPLE = {
    JORDAN: {"channel": "slack", "address": "D-JORDAN", "identity": "U-JORDAN"},
    PRIYA: {"channel": "slack", "address": "D-PRIYA"},
    OWNER: {"channel": "email", "address": "owner@client.example"},
}
SECRET = "Hargreaves boiler at 12 Acacia Road"


def request(tool: str = "calendar.book", marker: str = "a") -> dict:
    return {"agent": "receptionist", "requestId": marker * 64, "tool": tool, "arguments": {"who": SECRET}}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = pathlib.Path(self._tmp.name)
        self.root = base / "handoffs"
        self.audit_path = base / "audit.jsonl"
        self.events: list[tuple] = []
        self.directory = delivery.Directory(PEOPLE)

    def make(self, assignee: str = JORDAN, marker: str = "a", hours: int = 24) -> dict:
        return handoff.create(self.root, request(marker=marker), capability="calendar", reason="unbound", assignee=assignee,
                              deadline_hours=hours, now=T0)

    def complete(self, handoff_id, outcome, by, *, note=None, via=None):
        return govern.complete_handoff(self.root, handoff_id, outcome, by, audit_path=self.audit_path, note=note, via=via)

    def handler(self, **kwargs) -> "reply.HandoffReplies":
        return reply.HandoffReplies(self.root, self.directory, complete=self.complete,
                                    audit=lambda *event: self.events.append(event), **kwargs)

    def say(self, text, *, channel="slack", conversation="D-JORDAN", direct=True, sender="U-JORDAN", at=0, **kwargs):
        return self.handler(**kwargs).handle(channel, conversation, text, direct=direct, sender_id=sender, now=T0 + at)

    def ledger(self) -> list[dict]:
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()] if self.audit_path.exists() else []


class ParseTests(unittest.TestCase):
    def test_well_formed_replies(self):
        self.assertEqual(reply.parse("done aaaaaaaaaaaa"), {"outcome": "done", "reference": "aaaaaaaaaaaa", "note": ""})
        self.assertEqual(reply.parse("  Declined: AAAAAAAAAAAA  not our   policy\n today ")["note"], "not our policy today")
        self.assertEqual(reply.parse("declined aaaaaaaa")["outcome"], "declined")
        self.assertEqual(reply.parse("seen aaaaaaaaaaaa")["outcome"], "seen")
        self.assertEqual(reply.parse("ack aaaaaaaaaaaa")["outcome"], "seen")

    def test_anything_else_is_not_a_reply(self):
        for text in ("", "done", "done abc", "done not-a-ref-at-all", "please done aaaaaaaaaaaa", "ok aaaaaaaaaaaa", None, 7, "x" * 5000):
            self.assertIsNone(reply.parse(text), text)

    def test_note_is_bounded(self):
        self.assertEqual(len(reply.parse("done aaaaaaaaaaaa " + "n" * 3000)["note"]), reply.MAX_NOTE)


class DirectoryIdentityTests(unittest.TestCase):
    def test_identify_uses_channel_conversation_and_optional_identity(self):
        directory = delivery.Directory(PEOPLE)
        self.assertEqual(directory.identify("slack", "D-JORDAN", "U-JORDAN"), JORDAN)
        self.assertIsNone(directory.identify("slack", "D-JORDAN", "U-SOMEONE-ELSE"))
        self.assertIsNone(directory.identify("slack", "D-JORDAN"))
        self.assertEqual(directory.identify("slack", "D-PRIYA", "anything"), PRIYA)  # no recorded identity: conversation only
        self.assertIsNone(directory.identify("teams", "D-JORDAN", "U-JORDAN"))
        self.assertIsNone(directory.identify("slack", "", None))

    def test_a_shared_address_or_the_inbox_identifies_nobody(self):
        directory = delivery.Directory({
            JORDAN: {"channel": "slack", "address": "C-SHARED"}, PRIYA: {"channel": "slack", "address": "C-SHARED"},
            OWNER: {"channel": "inbox"},
        })
        self.assertIsNone(directory.identify("slack", "C-SHARED"))
        self.assertIsNone(directory.identify("inbox", ""))

    def test_signed_webchat_can_identify_a_named_inbox_person_only_with_explicit_identity(self):
        directory = delivery.Directory({
            JORDAN: {"channel": "inbox", "identity": JORDAN},
            PRIYA: {"channel": "inbox", "identity": PRIYA},
        })
        self.assertEqual(directory.lookup(JORDAN), ("inbox", ""))
        self.assertEqual(directory.identify("webchat", "signed-session-conversation", JORDAN), JORDAN)
        self.assertIsNone(directory.identify("webchat", "signed-session-conversation", OWNER))
        self.assertIsNone(directory.identify("webchat", "signed-session-conversation"))
        self.assertIsNone(directory.identify("webchat", "", JORDAN))
        self.assertIsNone(directory.identify("slack", "signed-session-conversation", JORDAN))
        self.assertIsNone(directory.identify("inbox", "signed-session-conversation", JORDAN))
        self.assertIsNone(delivery.Directory({JORDAN: {"channel": "inbox"}}).identify(
            "webchat", "signed-session-conversation", JORDAN,
        ))

    def test_duplicate_webchat_identity_is_ambiguous_and_never_grants_authority(self):
        directory = delivery.Directory({
            JORDAN: {"channel": "inbox", "identity": JORDAN},
            PRIYA: {"channel": "inbox", "identity": JORDAN},
        })
        self.assertIsNone(directory.identify("webchat", "signed-session-conversation", JORDAN))

    def test_identity_must_not_be_a_credential(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery.Directory({JORDAN: {"channel": "slack", "address": "D1", "identity": "xoxb-api_key-123"}})
        with self.assertRaises(delivery.DeliveryError):
            delivery.Directory({JORDAN: {"channel": "slack", "address": "D1", "identity": 7}})
        with self.assertRaises(delivery.DeliveryError):
            delivery.Directory({JORDAN: {"channel": "slack", "address": "D1", "secret": "x"}})


class ClosingTests(Case):
    def test_done_closes_under_the_verified_person_and_channel(self):
        document = self.make()
        result = self.say("done " + document["id"][:12] + " booked it")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["id"], document["id"])
        self.assertIn("carried out calendar.book", result["reply"])
        self.assertNotIn(SECRET, result["reply"])
        closed = handoff.closed(self.root)[0]
        self.assertEqual((closed["status"], closed["completedBy"], closed["completedVia"], closed["note"]), ("done", JORDAN, "slack", "booked it"))
        self.assertEqual(handoff.pending(self.root), [])
        event = self.ledger()[-1]
        self.assertEqual((event["event"], event["outcome"]), ("handoff", "done"))
        self.assertEqual(event["details"]["completedBy"], JORDAN)
        self.assertEqual(event["details"]["via"], "slack")
        self.assertEqual((event["ledger"]["tier"], event["ledger"]["provider"]), (4, "human"))

    def test_declined_is_recorded_as_not_done(self):
        document = self.make()
        result = self.say("declined " + document["id"][:12] + " not our policy")
        self.assertEqual(result["status"], "closed")
        self.assertIn("you declined", result["reply"])
        closed = handoff.closed(self.root)[0]
        self.assertEqual((closed["status"], closed["completedBy"], closed["note"]), ("declined", JORDAN, "not our policy"))
        self.assertIn("NOT done", handoff.describe(closed)["message"])

    def test_the_words_cannot_name_who_is_answering(self):
        document = self.make()
        self.say("done " + document["id"][:12] + f" by {OWNER} as {PRIYA}")
        self.assertEqual(handoff.closed(self.root)[0]["completedBy"], JORDAN)

    def test_a_second_answer_changes_nothing_and_says_what_was_recorded(self):
        document = self.make()
        self.say("declined " + document["id"][:12])
        again = self.say("done " + document["id"][:12], at=60)
        self.assertEqual(again["status"], "already-closed")
        self.assertIn("declined", again["reply"])
        self.assertEqual(len(handoff.closed(self.root)), 1)
        self.assertEqual(handoff.closed(self.root)[0]["status"], "declined")

    def test_a_late_answer_is_marked_late(self):
        document = self.make(hours=1)
        self.say("done " + document["id"][:12], at=2 * 3600)
        self.assertTrue(handoff.closed(self.root)[0]["late"])

    def test_a_human_task_step_resumes_with_the_note_as_data(self):
        envelope = {"taskId": "task-1", "authority": {"tools": ["calendar.read"]}}
        document = human_step.open_step(
            self.root, envelope, step="approve-discount", assignee=JORDAN, ask="Is a 10% discount fine?", context="repeat client",
            timeout_hours=24, fallback="fail", now=T0,
        )
        result = self.say("done " + document["id"][:12] + " yes, 10% is fine")
        self.assertEqual(result["status"], "closed")
        outcome = human_step.resolution(self.root, envelope, "approve-discount", now=T0 + 60)
        self.assertEqual((outcome["state"], outcome["resume"], outcome["by"], outcome["input"]), ("resumed", True, JORDAN, "yes, 10% is fine"))
        self.assertEqual(envelope["authority"], {"tools": ["calendar.read"]})  # the answer never widens authority
        event = self.ledger()[-1]
        self.assertEqual((event["tool"], event["details"]["completedBy"]), ("task.human-step", JORDAN))


class IdentityAndAuthorityTests(Case):
    def refused(self, status, **kwargs):
        document = self.make()
        result = self.say("done " + document["id"][:12], **kwargs)
        self.assertEqual(result["status"], status)
        self.assertEqual(result["reply"], reply._NEUTRAL if status != "not-direct" else result["reply"])
        self.assertEqual(handoff.closed(self.root), [])
        self.assertEqual(len(handoff.pending(self.root)), 1)
        return result

    def test_an_unknown_conversation_is_refused(self):
        self.refused("unknown-sender", conversation="D-STRANGER")

    def test_a_wrong_channel_is_refused(self):
        self.refused("unknown-sender", channel="teams")

    def test_a_wrong_sender_in_the_right_conversation_is_refused(self):
        self.refused("unknown-sender", sender="U-IMPOSTOR")

    def test_a_shared_channel_is_refused_even_for_the_right_person(self):
        self.refused("not-direct", direct=False)

    def test_another_person_cannot_close_it(self):
        self.refused("not-assignee", conversation="D-PRIYA", sender="U-PRIYA")

    def test_the_escalation_contact_can_close_only_once_overdue(self):
        document = self.make(hours=1)
        early = self.say("done " + document["id"][:12], channel="email", conversation=OWNER, sender=None, at=600, escalate_to=OWNER)
        self.assertEqual(early["status"], "not-assignee")
        late = self.say("done " + document["id"][:12], channel="email", conversation=OWNER, sender=None, at=2 * 3600, escalate_to=OWNER)
        self.assertEqual(late["status"], "closed")
        self.assertEqual(handoff.closed(self.root)[0]["completedBy"], OWNER)
        self.assertEqual(handoff.closed(self.root)[0]["completedVia"], "email")

    def test_without_an_escalation_contact_only_the_assignee_can_close(self):
        document = self.make(hours=1)
        result = self.say("done " + document["id"][:12], channel="email", conversation=OWNER, sender=None, at=2 * 3600)
        self.assertEqual(result["status"], "not-assignee")

    def test_unknown_short_and_ambiguous_references_all_get_the_neutral_reply(self):
        first, second = self.make(marker="a"), self.make(marker="b")
        for text in ("done " + "c" * 12, "done " + "a" * 8 + "ffff"):
            self.assertEqual(self.say(text)["status"], "no-match", text)
        # two open hand-offs sharing an 8 character prefix cannot be told apart, so neither is closed
        left = handoff.create(self.root, {"agent": "receptionist", "requestId": "1234abcd" + "0" * 56, "tool": "mail.send", "arguments": {}},
                              capability="mail", reason="unbound", assignee=JORDAN, deadline_hours=24, now=T0)
        right = handoff.create(self.root, {"agent": "receptionist", "requestId": "1234abcd" + "1" * 56, "tool": "mail.send", "arguments": {}},
                               capability="mail", reason="unbound", assignee=JORDAN, deadline_hours=24, now=T0)
        self.assertEqual(self.say("done 1234abcd")["status"], "no-match")
        self.assertEqual(self.say("done " + left["id"][:12])["status"], "closed")
        self.assertEqual(self.say("done " + right["id"][:12])["status"], "closed")
        self.assertEqual({first["id"], second["id"]}, {item["id"] for item in handoff.pending(self.root)})

    def test_garbage_gets_usage_and_changes_nothing(self):
        self.make()
        result = self.say("hello there")
        self.assertEqual(result["status"], "not-a-reply")
        self.assertIn("done <reference>", result["reply"])
        self.assertEqual(handoff.closed(self.root), [])

    def test_refusals_are_ledgered_without_the_words_or_the_arguments(self):
        document = self.make()
        self.say("done " + document["id"][:12] + " " + SECRET, conversation="D-STRANGER")
        self.say("done " + document["id"][:12] + " " + SECRET, conversation="D-PRIYA", sender="U-PRIYA")
        recorded = json.dumps(self.events)
        self.assertNotIn(SECRET, recorded)
        self.assertNotIn("Acacia", recorded)
        self.assertEqual([event[2]["status"] for event in self.events], ["unknown-sender", "not-assignee"])
        self.assertEqual(self.events[1][1], PRIYA)
        self.assertTrue(all(len(event[2]["reference"]) <= 12 for event in self.events))

    def test_the_reply_never_contains_the_action_arguments(self):
        document = self.make()
        for text in ("seen " + document["id"][:12], "done " + document["id"][:12]):
            self.assertNotIn(SECRET, self.say(text)["reply"])


class AcknowledgementTests(Case):
    def test_seen_records_an_acknowledgement_without_closing(self):
        document = self.make()
        result = self.say("seen " + document["id"][:12])
        self.assertEqual(result["status"], "acknowledged")
        self.assertIn("still waiting", result["reply"])
        stored = handoff.pending(self.root)[0]
        self.assertEqual(stored["status"], "pending")
        self.assertEqual((stored["acknowledged"]["by"], stored["acknowledged"]["via"]), (JORDAN, "slack"))
        self.assertEqual(handoff.closed(self.root), [])

    def test_seen_quiets_the_reminder_but_never_the_escalation(self):
        document = self.make(hours=24)
        before_deadline = T0 + 23 * 3600 + 1800
        self.assertEqual({n["kind"] for n in handoff.due_notices(self.root, now=before_deadline)}, {"assigned", "reminder"})
        self.say("seen " + document["id"][:12])
        self.assertEqual({n["kind"] for n in handoff.due_notices(self.root, now=before_deadline)}, {"assigned"})
        overdue = {n["kind"] for n in handoff.due_notices(self.root, now=T0 + 25 * 3600, escalate_to=OWNER)}
        self.assertIn("escalation", overdue)

    def test_a_repeat_acknowledgement_keeps_the_first(self):
        document = self.make()
        self.say("seen " + document["id"][:12], at=10)
        self.say("seen " + document["id"][:12], at=500)
        self.assertEqual(handoff.pending(self.root)[0]["acknowledged"]["at"], T0 + 10)

    def test_acknowledge_requires_a_pending_hand_off(self):
        with self.assertRaises(handoff.HandoffError):
            handoff.acknowledge(self.root, "f" * 64, JORDAN, via="slack")
        with self.assertRaises(handoff.HandoffError):
            handoff.acknowledge(self.root, "not-an-id", JORDAN, via="slack")


class CommandTests(Case):
    def run_command(self, *argv):
        import contextlib
        import io

        runtime = _load("runtime_handoff_under_test", "scripts/runtime-handoff.py")
        people = pathlib.Path(self._tmp.name) / "people.json"
        people.write_text(json.dumps(PEOPLE))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runtime.main([str(self.root), "--audit-path", str(self.audit_path), "reply", "--people", str(people), *argv])
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def test_reply_command_closes_and_ledgers_the_person_and_channel(self):
        document = self.make()
        code, result, _ = self.run_command("--channel", "slack", "--conversation", "D-JORDAN", "--sender", "U-JORDAN", "--direct",
                                           "done " + document["id"][:12] + " ok")
        self.assertEqual((code, result["status"]), (0, "closed"))
        self.assertEqual(handoff.closed(self.root)[0]["completedVia"], "slack")
        events = {event["event"]: event for event in self.ledger()}
        self.assertEqual(events["handoff"]["details"]["via"], "slack")
        self.assertEqual((events["operator-action"]["action"], events["operator-action"]["performedBy"]), ("handoff.reply", JORDAN))

    def test_reply_command_refuses_without_direct_or_with_the_wrong_sender(self):
        document = self.make()
        code, result, _ = self.run_command("--channel", "slack", "--conversation", "D-JORDAN", "--sender", "U-JORDAN",
                                           "done " + document["id"][:12])
        self.assertEqual((code, result["status"]), (1, "not-direct"))
        code, result, _ = self.run_command("--channel", "slack", "--conversation", "D-JORDAN", "--sender", "U-X", "--direct",
                                           "done " + document["id"][:12])
        self.assertEqual((code, result["status"]), (1, "unknown-sender"))
        self.assertEqual(handoff.closed(self.root), [])
        actions = [event.get("action") or event.get("event") for event in self.ledger()]
        self.assertEqual(actions.count("handoff.reply"), 2)


class SetupTests(Case):
    def test_setup_is_checked(self):
        with self.assertRaises(reply.ReplyError):
            reply.HandoffReplies(self.root, self.directory, complete="nope")
        with self.assertRaises(reply.ReplyError):
            reply.HandoffReplies(self.root, object(), complete=self.complete)

    def test_the_operator_path_still_closes_without_a_channel(self):
        document = self.make()
        closed = govern.complete_handoff(self.root, document["id"], "done", JORDAN, audit_path=self.audit_path)
        self.assertNotIn("completedVia", closed)
        self.assertNotIn("via", self.ledger()[-1]["details"])


if __name__ == "__main__":
    unittest.main()
