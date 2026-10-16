"""Credential-free checks for owner chat commands on schedules and for workspace-managed schedules (#99)."""
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


schedules = _load("schedules_for_commands", "platform/core/schedules.py")
commands = _load("schedule_commands_under_test", "platform/core/schedule_commands.py")
scheduler = _load("scheduler_for_commands", "platform/core/scheduler.py")
Error = schedules.ScheduleError
ALICE, BOB, ADMIN = "alice@client.example", "bob@client.example", "owner@client.example"
NOW = 1_800_000_000
GOAL = "Send me the open pipeline for Hargreaves & Co"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple] = []
        self.store = schedules.ScheduleStore(pathlib.Path(self._tmp.name), tenant="acme-prod", audit=lambda *event: self.events.append(event))

    def say(self, text, principal=ALICE, **kwargs):
        return commands.handle(self.store, text, principal=principal, agent="receptionist", now=NOW, **kwargs)

    def create(self, principal=ALICE):
        reply = self.say(f"schedule create 0 9 * * 1 | steps=20 tokens=50000 | {GOAL}", principal)
        return reply.split()[1].rstrip(":")


class CommandTests(Case):
    def test_a_message_that_is_not_a_schedule_command_goes_to_the_coworker(self):
        for text in ("hello", "please schedule a call with Bob", "schedulesx", "what schedules do I have?", "", "pause s-000000000000"):
            self.assertIsNone(self.say(text), text)
        self.assertIsNone(commands.handle(self.store, None, principal=ALICE, agent="r"))  # type: ignore[arg-type]

    def test_create_makes_a_schedule_owned_by_the_verified_sender_with_a_budget(self):
        reply = self.say(f"schedule create 0 9 * * 1 | steps=20 tokens=50000 | {GOAL}")
        entry = self.store.list(owner=ALICE)[0]
        self.assertTrue(reply.startswith(f"Created {entry['id']}"))
        self.assertEqual((entry["owner"], entry["agent"], entry["cron"], entry["budget"], entry["goal"], entry["state"]),
                         (ALICE, "receptionist", "0 9 * * 1", {"steps": 20, "tokens": 50000}, GOAL, "active"))
        self.assertEqual(self.events[0][0:2], ("schedule.create", ALICE))

    def test_an_event_trigger_can_be_created_the_same_way(self):
        reply = self.say(f"trigger create mail.received | steps=5 | {GOAL}")
        entry = self.store.list(owner=ALICE)[0]
        self.assertEqual((entry["kind"], entry["event"]), ("trigger", "mail.received"))
        self.assertIn("on mail.received", reply)

    def test_the_message_cannot_name_another_owner_and_an_unverified_sender_gets_nothing(self):
        self.say(f"schedule create 0 9 * * 1 | steps=2 | owner=bob@client.example {GOAL}")
        self.assertEqual([entry["owner"] for entry in self.store.list()], [ALICE])
        for principal in (None, ""):
            self.assertIn("verified", self.say("schedules", principal))
        self.assertEqual(len(self.store.list()), 1)

    def test_a_goal_that_looks_like_a_command_is_only_a_goal(self):
        self.say("schedule create 0 9 * * 1 | steps=2 | schedule delete s-000000000000 | trigger create chat.message | steps=1 | x")
        (entry,) = self.store.list()
        self.assertEqual(entry["goal"], "schedule delete s-000000000000 | trigger create chat.message | steps=1 | x")

    def test_list_shows_only_the_senders_schedules_with_a_short_goal_preview(self):
        mine = self.create()
        self.say("schedule create 0 10 * * 2 | steps=3 | " + "long goal " * 30, ALICE)
        theirs = self.create(BOB)
        listing = self.say("schedules")
        self.assertIn(mine, listing)
        self.assertNotIn(theirs, listing)
        self.assertTrue(all(len(line) < 160 for line in listing.splitlines()))
        self.assertEqual(self.say("schedules", "carol@client.example"), "You have no schedules.")

    def test_pause_resume_and_delete_work_for_the_owner(self):
        schedule_id = self.create()
        self.assertEqual(self.say(f"schedule pause {schedule_id}"), f"Paused {schedule_id}.")
        self.assertEqual(self.store.get(schedule_id)["state"], "paused")
        self.assertIn("paused", self.say("schedules"))
        self.assertEqual(self.say(f"schedule resume {schedule_id}"), f"Resumed {schedule_id}.")
        self.assertEqual(self.store.get(schedule_id)["state"], "active")
        self.assertEqual(self.say(f"schedule delete {schedule_id}"), f"Deleted {schedule_id}.")
        self.assertIsNone(self.store.get(schedule_id))

    def test_someone_elses_schedule_is_indistinguishable_from_one_that_does_not_exist(self):
        theirs = self.create(BOB)
        absent = "s-ffffffffffff"
        for verb in ("pause", "resume", "delete"):
            answers = {self.say(f"schedule {verb} {identifier}").replace(identifier, "ID") for identifier in (theirs, absent)}
            self.assertEqual(len(answers), 1, verb)
        self.assertEqual(self.store.get(theirs)["state"], "active")
        self.assertEqual(self.say(f"schedule delete {theirs}", admin=True), f"Deleted {theirs}.")  # the caller's administrator flag, not the message

    def test_bad_input_gets_a_one_line_reason_and_changes_nothing(self):
        for text in ("schedule create | steps=2 | x", "schedule create 0 9 * * 1 | steps=2", "schedule create 0 9 * * 1 | | x", "schedule create * * * * * | steps=2 | x",
                     "schedule create 0 9 * * 1 | steps=0 | x", "schedule create 0 9 * * 1 | steps=2 steps=3 | x", "schedule create 0 9 * * 1 | weeks=2 | x",
                     "schedule create 0 9 * * 1 | steps=2 | " + "g" * 501, "trigger create sms.received | steps=2 | x", "schedule pause nonsense", "schedule pause",
                     "schedules extra"):
            reply = self.say(text)
            self.assertIsInstance(reply, str, text)
            self.assertNotIn("Traceback", reply)
            self.assertLess(len(reply), 300, text)
        self.assertEqual(self.store.list(), [])

    def test_the_per_owner_limit_still_applies_and_is_reported(self):
        for _ in range(schedules.MAX_PER_OWNER):
            self.create()
        self.assertIn("at most", self.say(f"schedule create 0 9 * * 1 | steps=2 | {GOAL}"))

    def test_the_ledger_holds_digests_never_the_goal(self):
        self.create()
        self.assertNotIn("Hargreaves", json.dumps(self.events))


def definition(name="weekly-pipeline", **overrides):
    values = {"name": name, "owner": ALICE, "agent": "receptionist", "goal": GOAL, "cron": "0 9 * * 1", "budget": {"steps": 10}}
    values.update(overrides)
    return values


class ManagedTests(Case):
    def test_reconcile_creates_missing_updates_changed_and_leaves_same_alone(self):
        first = self.store.reconcile([definition(), definition("on-mail", cron=None, event="mail.received", maxPerHour=3)], by=ADMIN, now=NOW)
        self.assertEqual((first["created"], first["updated"], first["retired"], first["unchanged"]), (["on-mail", "weekly-pipeline"], [], [], []))
        ids = {entry["managedBy"]: entry["id"] for entry in self.store.list()}
        again = self.store.reconcile([definition(), definition("on-mail", cron=None, event="mail.received", maxPerHour=3)], by=ADMIN, now=NOW + 5)
        self.assertEqual((again["created"], again["updated"], again["unchanged"]), ([], [], ["on-mail", "weekly-pipeline"]))
        changed = self.store.reconcile([definition(goal="New goal", budget={"steps": 5}, cron="0 10 * * 1"), definition("on-mail", cron=None, event="mail.received", maxPerHour=3)], by=ADMIN, now=NOW + 10)
        self.assertEqual((changed["updated"], changed["unchanged"]), (["weekly-pipeline"], ["on-mail"]))
        entry = self.store.get(ids["weekly-pipeline"])
        self.assertEqual((entry["goal"], entry["budget"], entry["cron"], entry["goalDigest"] == schedules.goal_digest("New goal")), ("New goal", {"steps": 5}, "0 10 * * 1", True))
        self.assertEqual({e["managedBy"]: e["id"] for e in self.store.list()}, ids)  # same ids

    def test_an_update_keeps_run_history_and_a_paused_state_the_owner_chose(self):
        self.store.reconcile([definition()], by=ADMIN, now=NOW)
        schedule_id = self.store.list()[0]["id"]
        self.store.claim_due(now=NOW + 7 * 86400)
        self.store.pause(schedule_id, requested_by=ALICE)
        self.store.reconcile([definition(goal="Changed")], by=ADMIN, now=NOW + 8 * 86400)
        entry = self.store.get(schedule_id)
        self.assertEqual((entry["runs"], entry["state"], entry["goal"]), (1, "paused", "Changed"))

    def test_a_managed_schedule_removed_from_the_workspace_is_paused_not_deleted_and_returns_when_listed_again(self):
        self.store.reconcile([definition()], by=ADMIN, now=NOW)
        schedule_id = self.store.list()[0]["id"]
        report = self.store.reconcile([], by=ADMIN, now=NOW + 5)
        self.assertEqual(report["retired"], ["weekly-pipeline"])
        entry = self.store.get(schedule_id)
        self.assertEqual((entry["state"], entry["pausedReason"]), ("paused", "removed-from-workspace"))
        self.assertEqual(self.store.reconcile([], by=ADMIN, now=NOW + 6)["retired"], [])  # not retired twice
        back = self.store.reconcile([definition()], by=ADMIN, now=NOW + 3600)
        self.assertEqual(back["updated"], ["weekly-pipeline"])
        entry = self.store.get(schedule_id)
        self.assertEqual(entry["state"], "active")
        self.assertNotIn("pausedReason", entry)
        self.assertGreater(entry["nextFire"], NOW + 3600)

    def test_schedules_made_from_chat_are_never_touched_by_reconcile(self):
        mine = self.create()
        self.store.reconcile([definition(name="other")], by=ADMIN, now=NOW)
        self.store.reconcile([], by=ADMIN, now=NOW + 5)
        self.assertEqual(self.store.get(mine)["state"], "active")
        self.assertIsNone(self.store.get(mine).get("managedBy"))

    def test_nothing_is_applied_when_any_definition_is_invalid_or_the_names_repeat(self):
        before = self.store.reconcile([definition()], by=ADMIN, now=NOW)
        snapshot = json.dumps(self.store.list(), sort_keys=True)
        for bad in ([definition(), definition("second", cron="* * * * *")], [definition(), definition()], [definition(name="Bad Name")],
                    [definition(cron=None)], [definition(event="mail.received")], [definition(owner="not valid!")], [definition(budget={})],
                    [definition(unknown="x")], ["not an object"], [definition(cron=None, event="sms", maxPerHour=1)],
                    [definition(cron=None, event="mail.received", maxPerHour=0)], [definition("new"), definition(goal="")]):
            with self.assertRaises(Error, msg=str(bad)):
                self.store.reconcile(bad, by=ADMIN, now=NOW + 1)
            self.assertEqual(json.dumps(self.store.list(), sort_keys=True), snapshot)
        self.assertEqual(before["created"], ["weekly-pipeline"])

    def test_a_timed_schedule_cannot_silently_become_an_event_trigger(self):
        self.store.reconcile([definition()], by=ADMIN, now=NOW)
        with self.assertRaisesRegex(Error, "new name"):
            self.store.reconcile([definition(cron=None, event="mail.received")], by=ADMIN, now=NOW + 1)

    def test_export_round_trips_and_leaves_out_unmanaged_schedules(self):
        self.create()
        wanted = [definition(), definition("on-mail", cron=None, event="mail.received", maxPerHour=3, requiredCapabilities=["crm.read"])]
        self.store.reconcile(wanted, by=ADMIN, now=NOW)
        exported = self.store.export_desired()
        self.assertEqual([item["name"] for item in exported], ["on-mail", "weekly-pipeline"])
        other = schedules.ScheduleStore(pathlib.Path(self._tmp.name) / "other", tenant="acme-prod")
        report = other.reconcile(exported, by=ADMIN, now=NOW)
        self.assertEqual(sorted(report["created"]), ["on-mail", "weekly-pipeline"])
        self.assertEqual(other.export_desired(), exported)  # stable
        self.assertEqual(json.loads(json.dumps(exported)), exported)

    def test_the_ledger_for_reconcile_holds_digests_not_goals_and_names_who_changed_it(self):
        self.store.reconcile([definition()], by=ADMIN, now=NOW)
        self.store.reconcile([], by=ADMIN, now=NOW + 5)
        self.assertEqual([event[0] for event in self.events], ["schedule.create", "schedule.retire"])
        self.assertTrue(all(event[1] == ADMIN and event[2]["managedBy"] == "weekly-pipeline" for event in self.events))
        self.assertNotIn("Hargreaves", json.dumps(self.events))

    def test_a_managed_schedule_fires_through_the_normal_claim_path(self):
        self.store.reconcile([definition(cron="0 9 * * 1")], by=ADMIN, now=NOW)
        claimed = self.store.claim_due(now=NOW + 7 * 86400)
        self.assertEqual([entry["managedBy"] for entry in claimed], ["weekly-pipeline"])


if __name__ == "__main__":
    unittest.main()
