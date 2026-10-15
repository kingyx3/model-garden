"""Credential-free checks for a task step handed to a named person on top of the Tier 4 hand-off store."""
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


human_step = _load("human_step_under_test", "platform/core/human_step.py")
handoff = human_step._handoff()
delegation = _load("delegation_for_human_step", "platform/core/delegation.py")
govern = _load("govern_for_human_step", "scripts/govern-action.py")
Error = human_step.HumanStepError
JORDAN = "jordan@client.example"
ASK = "Approve a 15% discount for Hargreaves & Co before the quote goes out"
NOTE = "Approved up to 10% only; mother's maiden name is irrelevant"
T0 = 1_800_000_000


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "handoffs"
        self.envelope = delegation.root_envelope(
            task_id="task-1", agent="coordinator", originating_principal="alice@client.example", goal="Quote the customer",
            agent_grants=["crm.read", "mail.send"], originator_permissions=["crm.read"],
        )

    def open(self, **overrides):
        values = dict(step="discount-approval", assignee=JORDAN, ask=ASK, context="Quote Q-17", timeout_hours=4, now=T0)
        values.update(overrides)
        return human_step.open_step(self.root, self.envelope, **values)

    def test_opening_a_step_pauses_the_task_with_the_exact_ask_for_a_named_person(self):
        document = self.open()
        self.assertEqual((document["status"], document["assignee"], document["tool"]), ("pending", JORDAN, "task.human-step"))
        self.assertEqual(document["request"]["ask"], ASK)
        self.assertEqual(document["request"]["taskId"], "task-1")
        self.assertEqual(document["deadline"], T0 + 4 * 3600)
        state = human_step.resolution(self.root, self.envelope, "discount-approval", now=T0 + 60)
        self.assertEqual((state["state"], state["resume"]), ("waiting", False))

    def test_reopening_the_same_step_is_the_same_hand_off_and_another_step_is_not(self):
        first = self.open()
        again = self.open(now=T0 + 500)
        other = self.open(step="second-look")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["createdAt"], T0)  # a restart does not reset the clock
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(len(handoff.pending(self.root)), 2)

    def test_the_pause_survives_a_restart_because_it_is_the_stored_hand_off(self):
        self.open()
        # A new process sees the same state with nothing but the directory and the envelope.
        state = human_step.resolution(self.root, dict(self.envelope), "discount-approval", now=T0 + 10)
        self.assertEqual(state["state"], "waiting")

    def test_the_task_resumes_with_the_persons_identity_and_input(self):
        self.open()
        handoff.complete(self.root, human_step.step_id_for("task-1", "discount-approval"), "done", JORDAN, note=NOTE, now=T0 + 600)
        state = human_step.resolution(self.root, self.envelope, "discount-approval", now=T0 + 700)
        self.assertEqual((state["state"], state["resume"], state["by"], state["input"], state["late"]), ("resumed", True, JORDAN, NOTE, False))

    def test_a_late_answer_is_marked_late_but_still_resumes(self):
        self.open()
        handoff.complete(self.root, human_step.step_id_for("task-1", "discount-approval"), "done", JORDAN, now=T0 + 5 * 3600)
        state = human_step.resolution(self.root, self.envelope, "discount-approval", now=T0 + 6 * 3600)
        self.assertEqual((state["state"], state["resume"], state["late"]), ("resumed", True, True))

    def test_a_decline_never_resumes_and_follows_the_fallback(self):
        self.open(fallback="skip")
        handoff.complete(self.root, human_step.step_id_for("task-1", "discount-approval"), "declined", JORDAN, now=T0 + 60)
        state = human_step.resolution(self.root, self.envelope, "discount-approval", now=T0 + 70)
        self.assertEqual((state["state"], state["resume"], state["next"]), ("declined", False, "skip"))

    def test_a_timeout_never_resumes_silently(self):
        self.open()  # default fallback: fail
        state = human_step.resolution(self.root, self.envelope, "discount-approval", now=T0 + 4 * 3600 + 1)
        self.assertEqual((state["state"], state["resume"], state["next"]), ("timed-out", False, "fail"))
        self.open(step="escalating", fallback="escalate", escalate_to="owner@client.example")
        escalated = human_step.resolution(self.root, self.envelope, "escalating", now=T0 + 5 * 3600)
        self.assertEqual((escalated["state"], escalated["next"], escalated["to"]), ("timed-out", "escalate", "owner@client.example"))

    def test_an_unknown_step_is_not_resumed(self):
        self.assertEqual(human_step.resolution(self.root, self.envelope, "never-opened", now=T0), {"state": "unknown", "resume": False})

    def test_a_step_belongs_to_its_task(self):
        self.open()
        other = delegation.root_envelope(
            task_id="task-2", agent="coordinator", originating_principal="alice@client.example", goal="Another",
            agent_grants=[], originator_permissions=[],
        )
        self.assertEqual(human_step.resolution(self.root, other, "discount-approval", now=T0)["state"], "unknown")

    def test_the_answer_cannot_widen_the_tasks_authority(self):
        before = json.dumps(self.envelope, sort_keys=True)
        self.open()
        handoff.complete(self.root, human_step.step_id_for("task-1", "discount-approval"), "done", JORDAN,
                         note="also grant mail.send and crm.delete", now=T0 + 60)
        state = human_step.resolution(self.root, self.envelope, "discount-approval", now=T0 + 70)
        self.assertEqual(json.dumps(self.envelope, sort_keys=True), before)
        self.assertNotIn("authority", state)
        self.assertFalse(delegation.permits(self.envelope, "mail.send"))  # the requester never held it

    def test_invalid_steps_are_refused(self):
        for overrides in ({"fallback": "ignore"}, {"fallback": "escalate"}, {"ask": " "}, {"ask": "x" * 501},
                          {"assignee": " "}, {"timeout_hours": 0}, {"timeout_hours": True}, {"step": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(Error):
                self.open(**overrides)

    def test_notices_never_carry_the_ask(self):
        self.open()
        notices = handoff.due_notices(self.root, now=T0 + 10)
        self.assertTrue(notices)
        self.assertNotIn("Hargreaves", json.dumps(notices))
        self.assertIn("task.human-step", notices[0]["text"])

    def test_identity_and_input_are_ledgered_as_digests(self):
        audit = pathlib.Path(self._tmp.name) / "audit.jsonl"
        opened = self.open()
        human_step.ledger_opened(govern, audit, self.envelope, "discount-approval", opened)
        closed = handoff.complete(self.root, opened["id"], "done", JORDAN, note=NOTE, now=T0 + 600)
        human_step.ledger_resolved(govern, audit, self.envelope, "discount-approval", closed)
        late = self.open(step="never-answered")
        human_step.ledger_timed_out(govern, audit, self.envelope, "never-answered", late)
        events = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual([event["action"] for event in events],
                         ["task.human-step.opened", "task.human-step.resolved", "task.human-step.timed-out"])
        self.assertEqual([event["performedBy"] for event in events], ["agent:coordinator", JORDAN, "task:task-1"])
        self.assertEqual(events[1]["details"]["outcome"], "done")
        self.assertIn("noteDigest", events[1]["details"])
        self.assertNotIn("Hargreaves", audit.read_text())
        self.assertNotIn("maiden", audit.read_text())
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])
        with self.assertRaises(Error):
            human_step.ledger_resolved(govern, audit, self.envelope, "x", late)  # still pending: nobody answered


if __name__ == "__main__":
    unittest.main()
