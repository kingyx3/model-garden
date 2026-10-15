"""Credential-free checks for scheduled coworker tasks: owner, budget, cron arithmetic and owner-only control."""
from __future__ import annotations

import datetime as dt
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


schedules = _load("schedules_under_test", "platform/core/schedules.py")
delegation = _load("delegation_for_schedules", "platform/core/delegation.py")
govern = _load("govern_for_schedules", "scripts/govern-action.py")
Error = schedules.ScheduleError
ALICE, BOB, ADMIN = "alice@client.example", "bob@client.example", "owner@client.example"
GOAL = "Summarise the open pipeline for Hargreaves & Co and email it to Alice"


def utc(*args: int) -> int:
    return int(dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp())


NOW = utc(2026, 10, 15, 11, 0)  # a Thursday


class CronCase(unittest.TestCase):
    def test_next_fire_for_common_expressions(self):
        self.assertEqual(schedules.next_fire("0 9 * * 1", NOW), utc(2026, 10, 19, 9, 0))  # Monday 09:00
        self.assertEqual(schedules.next_fire("30 14 * * *", NOW), utc(2026, 10, 15, 14, 30))
        self.assertEqual(schedules.next_fire("30 10 * * *", NOW), utc(2026, 10, 16, 10, 30))
        self.assertEqual(schedules.next_fire("0 8 1 * *", NOW), utc(2026, 11, 1, 8, 0))
        self.assertEqual(schedules.next_fire("0 9 * * 7", NOW), utc(2026, 10, 18, 9, 0))  # 7 is Sunday

    def test_the_slot_is_strictly_after_the_reference_time(self):
        self.assertEqual(schedules.next_fire("0 11 * * *", NOW), utc(2026, 10, 16, 11, 0))

    def test_day_of_month_or_day_of_week_when_both_are_restricted(self):
        # Standard cron: the 13th OR a Friday. Friday 16 October comes first.
        self.assertEqual(schedules.next_fire("0 0 13 * 5", NOW), utc(2026, 10, 16, 0, 0))

    def test_ranges_lists_and_steps(self):
        self.assertEqual(schedules.next_fire("0,30 9-17/4 * * 1-5", NOW), utc(2026, 10, 15, 13, 0))
        self.assertEqual(schedules.next_fire("*/20 * * * *", NOW), utc(2026, 10, 15, 11, 20))

    def test_an_impossible_date_never_fires(self):
        self.assertIsNone(schedules.next_fire("0 0 31 2 *", NOW))

    def test_malformed_expressions_are_refused(self):
        for bad in ("* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "*/0 * * * *", "a b c d e", "5-1 * * * *", "", "* * * * * *"):
            with self.subTest(bad=bad), self.assertRaises(Error):
                schedules.parse_cron(bad)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "schedules"
        self.events: list[tuple] = []

    def store(self, tenant: str = "acme-prod") -> "schedules.ScheduleStore":
        return schedules.ScheduleStore(self.root, tenant=tenant, audit=lambda *event: self.events.append(event))

    def create(self, store=None, **overrides):
        values = dict(owner=ALICE, agent="receptionist", goal=GOAL, cron="0 9 * * 1", budget={"steps": 20, "costCents": 50}, now=NOW)
        values.update(overrides)
        return (store or self.store()).create(**values)

    def test_a_schedule_has_a_named_owner_a_budget_and_a_next_slot(self):
        entry = self.create(required_capabilities=["calendar.read"])
        self.assertEqual((entry["owner"], entry["state"], entry["nextFire"]), (ALICE, "active", utc(2026, 10, 19, 9, 0)))
        self.assertEqual(entry["budget"], {"steps": 20, "costCents": 50})
        self.assertEqual(entry["requiredCapabilities"], ["calendar.read"])

    def test_a_budget_is_required_and_positive(self):
        for budget in (None, {}, {"steps": 0}, {"steps": -1}, {"steps": True}, {"dollars": 3}, "lots"):
            with self.subTest(budget=budget), self.assertRaises(Error):
                self.create(budget=budget)

    def test_other_invalid_input_is_refused(self):
        for overrides in ({"owner": ""}, {"owner": "a b"}, {"agent": "../x"}, {"goal": " "}, {"goal": "x" * 501},
                          {"cron": "0 0 31 2 *"}, {"required_capabilities": ["not a tool"]}):
            with self.subTest(overrides=overrides), self.assertRaises(Error):
                self.create(**overrides)

    def test_a_schedule_cannot_fire_more_often_than_every_fifteen_minutes(self):
        for cron in ("* * * * *", "*/5 * * * *", "0,10 * * * *", "*/14 * * * *"):
            with self.subTest(cron=cron), self.assertRaises(Error):
                self.create(cron=cron)
        self.create(cron="*/15 * * * *")
        self.create(cron="0,30 9-17 * * 1-5")

    def test_owners_list_their_own_and_only_the_owner_or_an_admin_changes_one(self):
        store = self.store()
        mine, theirs = self.create(store), self.create(store, owner=BOB)
        self.assertEqual([item["id"] for item in store.list(owner=ALICE)], [mine["id"]])
        self.assertEqual(len(store.list()), 2)
        with self.assertRaises(Error):
            store.pause(mine["id"], requested_by=BOB)
        with self.assertRaises(Error):
            store.delete(mine["id"], requested_by=BOB)
        self.assertEqual(store.pause(mine["id"], requested_by=ALICE)["state"], "paused")
        self.assertEqual(store.resume(mine["id"], requested_by=ADMIN, admin=True, now=NOW)["state"], "active")
        store.delete(theirs["id"], requested_by=ADMIN, admin=True)
        store.delete(mine["id"], requested_by=ALICE)
        self.assertEqual(store.list(), [])
        with self.assertRaises(Error):
            store.pause("s-missing", requested_by=ALICE)

    def test_a_missed_slot_fires_once_and_a_paused_schedule_never(self):
        store = self.store()
        entry = self.create(store, cron="0 9 * * *", now=utc(2026, 10, 15, 8, 0))
        self.assertEqual(store.due(now=utc(2026, 10, 15, 8, 59)), [])
        # The host was down for three days: one firing, not one per missed slot.
        later = utc(2026, 10, 18, 12, 0)
        self.assertEqual([item["id"] for item in store.due(now=later)], [entry["id"]])
        fired = store.mark_fired(entry["id"], now=later)
        self.assertEqual((fired["runs"], fired["nextFire"]), (1, utc(2026, 10, 19, 9, 0)))
        self.assertEqual(store.due(now=later), [])
        store.pause(entry["id"], requested_by=ALICE)
        self.assertEqual(store.due(now=utc(2026, 10, 20, 12, 0)), [])
        with self.assertRaises(Error):
            store.mark_fired(entry["id"], now=utc(2026, 10, 20, 12, 0))

    def test_resuming_after_a_long_pause_does_not_replay_old_slots(self):
        store = self.store()
        entry = self.create(store, cron="0 9 * * *", now=utc(2026, 10, 15, 8, 0))
        store.pause(entry["id"], requested_by=ALICE)
        resumed = store.resume(entry["id"], requested_by=ALICE, now=utc(2026, 11, 1, 12, 0))
        self.assertEqual(resumed["nextFire"], utc(2026, 11, 2, 9, 0))
        self.assertEqual(store.due(now=utc(2026, 11, 1, 12, 0)), [])

    def test_the_owner_limit_and_tenant_isolation(self):
        store = self.store()
        for _ in range(schedules.MAX_PER_OWNER):
            self.create(store)
        with self.assertRaises(Error):
            self.create(store)
        self.create(store, owner=BOB)  # another owner is unaffected
        other = self.store("globex-prod")
        self.assertEqual(other.list(), [])
        (self.root / "globex-prod.schedules.json").write_text((self.root / "acme-prod.schedules.json").read_text())
        with self.assertRaises(Error):
            other.list()  # another tenant's file is refused, not read
        with self.assertRaises(Error):
            schedules.ScheduleStore(self.root, tenant="Bad Tenant")

    def test_files_are_private(self):
        self.create()
        self.assertEqual((self.root / "acme-prod.schedules.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_changes_are_ledgered_as_digests_never_the_goal(self):
        store = self.store()
        entry = self.create(store)
        store.pause(entry["id"], requested_by=ALICE)
        store.resume(entry["id"], requested_by=ALICE, now=NOW)
        store.mark_fired(entry["id"], now=utc(2026, 10, 19, 9, 0))
        store.delete(entry["id"], requested_by=ADMIN, admin=True)
        self.assertEqual([event[0] for event in self.events],
                         ["schedule.create", "schedule.pause", "schedule.resume", "schedule.fire", "schedule.delete"])
        self.assertEqual(self.events[0][1], ALICE)
        self.assertEqual(self.events[3][1], f"schedule:{entry['id']}")
        self.assertEqual(self.events[-1][1], ADMIN)
        self.assertTrue(self.events[-1][2]["admin"])
        self.assertNotIn("Hargreaves", json.dumps(self.events))
        self.assertEqual(self.events[0][2]["goalDigest"], schedules.goal_digest(GOAL))
        audit = pathlib.Path(self._tmp.name) / "audit.jsonl"
        for action, by, details in self.events:  # the real ledger accepts every event
            govern.record_operator_action(audit, action, by, details)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])
        self.assertNotIn("Hargreaves", audit.read_text())


class RunAuthorityCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = schedules.ScheduleStore(pathlib.Path(self._tmp.name), tenant="acme-prod")

    def entry(self, required):
        return self.store.create(owner=ALICE, agent="receptionist", goal=GOAL, cron="0 9 * * 1",
                                 budget={"steps": 10}, required_capabilities=required, now=NOW)

    def test_a_run_is_bounded_by_the_owners_permissions_and_the_schedule_budget(self):
        entry = self.entry(["crm.read"])
        envelope = schedules.run_envelope(
            entry, agent_grants=["crm.read", "crm.create", "mail.send"], owner_permissions=["crm.read", "crm.create"]
        )
        self.assertEqual(envelope["originatingPrincipal"], ALICE)
        self.assertEqual(envelope["authority"], ["crm.create", "crm.read"])  # mail.send is not the owner's
        self.assertEqual(envelope["budget"], {"steps": 10})
        self.assertTrue(delegation.permits(envelope, "crm.read"))
        self.assertFalse(delegation.permits(envelope, "mail.send"))
        self.assertEqual(envelope["taskId"], f"{entry['id']}:run-0")

    def test_losing_a_permission_stops_the_next_run_rather_than_widening_it(self):
        entry = self.entry(["crm.read"])
        schedules.run_envelope(entry, agent_grants=["crm.read"], owner_permissions=["crm.read"])
        with self.assertRaises(Error):
            schedules.run_envelope(entry, agent_grants=["crm.read"], owner_permissions=[])
        with self.assertRaises(Error):
            schedules.run_envelope(entry, agent_grants=[], owner_permissions=["crm.read"])

    def test_each_run_is_its_own_task(self):
        entry = self.entry([])
        first = schedules.run_envelope(entry, agent_grants=[], owner_permissions=[])
        fired = self.store.mark_fired(entry["id"], now=utc(2026, 10, 19, 9, 0))
        second = schedules.run_envelope(fired, agent_grants=[], owner_permissions=[])
        self.assertNotEqual(first["taskId"], second["taskId"])


if __name__ == "__main__":
    unittest.main()
