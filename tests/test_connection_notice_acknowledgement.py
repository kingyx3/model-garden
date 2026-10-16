"""Credential-free checks for acknowledging connection notices and escalating the ones nobody acknowledges (#86)."""
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


notices = _load("connection_notices_for_ack", "platform/core/connection_notices.py")
delivery = _load("notice_delivery_for_ack", "platform/core/notice_delivery.py")
NOW = 1_800_000_000
HOUR = 3600
OWNER, BOSS, STRANGER = "owner@client.example", "manager@client.example", "stranger@client.example"
CONNECTORS = {"calendar": {"provider": "google", "tier": 1, "owner": OWNER, "token_ref": "secret://google/dev"}}
HEALTH = {"calendar": {"family": "calendar", "health": "failed", "updatedBy": "op@platform.example", "updatedAt": "2026-10-15T10:00:00Z", "reason": "r"}}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple] = []
        self.store = notices.NoticeStore(pathlib.Path(self._tmp.name) / "governance", audit=lambda *event: self.events.append(event))

    def due(self, at=0, health=None, connectors=None, **kwargs):
        return self.store.due(HEALTH if health is None else health, connectors or CONNECTORS, (), now=NOW + at, **kwargs)

    def told(self, at=0, **kwargs):
        (notice,) = self.due(at, **kwargs)
        self.store.mark_notified(notice["key"], via="slack", now=NOW + at)
        return notice


class EscalationTests(Case):
    def test_an_unacknowledged_notice_escalates_once_after_the_window(self):
        notice = self.told(escalate_to=BOSS)
        self.assertEqual(self.due(23 * HOUR, escalate_to=BOSS), [])
        (escalation,) = self.due(25 * HOUR, escalate_to=BOSS)
        self.assertEqual((escalation["kind"], escalation["to"], escalation["unowned"]), ("connection-failed-unacknowledged", BOSS, False))
        self.assertNotEqual(escalation["key"], notice["key"])
        self.assertIn("has not acknowledged it", escalation["text"])
        self.assertIn("calendar connection is failed", escalation["text"])
        # offered again until it is itself delivered, then never again
        self.assertEqual(len(self.due(26 * HOUR, escalate_to=BOSS)), 1)
        self.store.mark_notified(escalation["key"], via="email", now=NOW + 26 * HOUR)
        self.assertEqual(self.due(60 * HOUR, escalate_to=BOSS), [])

    def test_an_acknowledged_notice_never_escalates(self):
        notice = self.told(escalate_to=BOSS)
        result = self.store.acknowledge(notice["key"], by=OWNER, via="slack", now=NOW + HOUR)
        self.assertFalse(result["duplicate"])
        self.assertEqual(self.due(48 * HOUR, escalate_to=BOSS), [])
        self.assertEqual(self.events[-1], ("connection.notice-acknowledged", OWNER, {"noticeKey": notice["key"], "kind": "connection-failed", "via": "slack"}))

    def test_nothing_escalates_without_an_escalation_contact_or_to_the_owner_themselves(self):
        self.told()
        self.assertEqual(self.due(72 * HOUR), [])
        self.assertEqual(self.due(72 * HOUR, escalate_to=OWNER), [])

    def test_an_undelivered_notice_does_not_escalate_it_is_simply_still_due(self):
        (first,) = self.due(escalate_to=BOSS)
        (again,) = self.due(30 * HOUR, escalate_to=BOSS)
        self.assertEqual(first["key"], again["key"])
        self.assertEqual(again["kind"], "connection-failed")

    def test_the_window_is_configurable_and_bounded(self):
        self.told(escalate_to=BOSS)
        self.assertEqual(len(self.due(2 * HOUR, escalate_to=BOSS, acknowledge_within_seconds=HOUR)), 1)
        with self.assertRaises(notices.NoticeError):
            self.due(0, escalate_to=BOSS, acknowledge_within_seconds=5)
        with self.assertRaises(notices.NoticeError):
            self.due(0, escalate_to=BOSS, acknowledge_within_seconds=True)

    def test_a_cleared_condition_forgets_the_escalation_so_a_recurrence_is_told_again(self):
        self.told(escalate_to=BOSS)
        (escalation,) = self.due(25 * HOUR, escalate_to=BOSS)
        self.store.mark_notified(escalation["key"], via="email", now=NOW + 25 * HOUR)
        healthy = {"calendar": {**HEALTH["calendar"], "health": "healthy"}}
        self.assertEqual(self.due(26 * HOUR, health=healthy, escalate_to=BOSS), [])
        recurrence = {"calendar": {**HEALTH["calendar"], "updatedAt": "2026-10-20T10:00:00Z"}}
        (fresh,) = self.due(27 * HOUR, health=recurrence, escalate_to=BOSS)
        self.assertEqual(fresh["kind"], "connection-failed")

    def test_the_escalation_carries_no_secret_or_reason(self):
        self.told(escalate_to=BOSS)
        (escalation,) = self.due(25 * HOUR, escalate_to=BOSS)
        blob = json.dumps(escalation)
        self.assertNotIn("secret://", blob)
        self.assertNotIn("token_ref", blob)

    def test_grant_notices_escalate_too(self):
        grant = {"id": "g1", "agent": "receptionist", "connectionId": "conn-1", "family": "calendar", "owner": OWNER,
                 "health": "failed"}
        (notice,) = self.store.due({}, {}, [grant], now=NOW, escalate_to=BOSS)
        self.store.mark_notified(notice["key"], via="slack", now=NOW)
        (escalation,) = self.store.due({}, {}, [grant], now=NOW + 25 * HOUR, escalate_to=BOSS)
        self.assertEqual((escalation["kind"], escalation["agent"], escalation["connectionId"]), ("grant-failed-unacknowledged", "receptionist", "conn-1"))


class AcknowledgementTests(Case):
    def test_only_the_recipient_or_the_escalation_contact_can_acknowledge(self):
        notice = self.told(escalate_to=BOSS)
        with self.assertRaises(notices.NoticeError):
            self.store.acknowledge(notice["key"], by=STRANGER, via="slack", now=NOW, escalate_to=BOSS)
        with self.assertRaises(notices.NoticeError):
            self.store.acknowledge(notice["key"], by=BOSS, via="slack", now=NOW)  # not told this is theirs without being named
        self.store.acknowledge(notice["key"], by=BOSS, via="email", now=NOW, escalate_to=BOSS)
        self.assertEqual(self.due(48 * HOUR, escalate_to=BOSS), [])

    def test_an_undelivered_or_unknown_notice_cannot_be_acknowledged(self):
        (notice,) = self.due()
        with self.assertRaises(notices.NoticeError):
            self.store.acknowledge(notice["key"], by=OWNER, via="slack", now=NOW)  # offered, never delivered
        with self.assertRaises(notices.NoticeError):
            self.store.acknowledge("f" * 64, by=OWNER, via="slack", now=NOW)
        with self.assertRaises(notices.NoticeError):
            self.store.acknowledge("nope", by=OWNER, via="slack", now=NOW)

    def test_a_repeat_keeps_the_first_acknowledgement_and_ledgers_once(self):
        notice = self.told()
        self.store.acknowledge(notice["key"], by=OWNER, via="slack", now=NOW + 10)
        again = self.store.acknowledge(notice["key"], by=OWNER, via="email", now=NOW + 500)
        self.assertTrue(again["duplicate"])
        self.assertEqual((again["via"], again["at"]), ("slack", NOW + 10))
        self.assertEqual(len([event for event in self.events if event[0] == "connection.notice-acknowledged"]), 1)

    def test_inputs_are_validated(self):
        notice = self.told()
        for kwargs in ({"by": "bad name!", "via": "slack"}, {"by": OWNER, "via": ""}, {"by": OWNER, "via": "x" * 200}):
            with self.assertRaises(notices.NoticeError):
                self.store.acknowledge(notice["key"], now=NOW, **kwargs)

    def test_an_acknowledgement_does_not_hide_the_condition_from_the_inbox_feed(self):
        notice = self.told()
        self.store.acknowledge(notice["key"], by=OWNER, via="slack", now=NOW)
        active = notices.current_notices(HEALTH, CONNECTORS, now=NOW)
        self.assertEqual([item["key"] for item in active], [notice["key"]])  # still an active condition

    def test_an_older_state_file_without_acknowledgements_still_loads(self):
        notice = self.told()
        path = self.store.path
        state = json.loads(path.read_text())
        state.pop("acknowledged")
        path.write_text(json.dumps(state))
        self.store.acknowledge(notice["key"], by=OWNER, via="slack", now=NOW)
        self.assertIn(notice["key"], json.loads(path.read_text())["acknowledged"])


class DeliveryTests(Case):
    def test_the_delivery_runner_carries_the_escalation_to_the_contacts_own_channel(self):
        directory = delivery.Directory({OWNER: {"channel": "slack", "address": "D-OWNER"}, BOSS: {"channel": "email", "address": BOSS}})
        sent: list[tuple[str, str]] = []
        senders = {"slack": lambda address, text: sent.append((address, text)) or "r1", "email": lambda address, text: sent.append((address, text)) or "r2"}

        def run(at):
            source = delivery.connection_source(self.store, HEALTH, CONNECTORS, now=NOW + at, escalate_to=BOSS)
            return delivery.deliver([source], directory, senders)

        self.assertEqual([row["status"] for row in run(0)], ["delivered"])
        self.assertEqual([row["status"] for row in run(HOUR)], [])
        results = run(25 * HOUR)
        self.assertEqual([(row["kind"], row["status"]) for row in results], [("connection-failed-unacknowledged", "delivered")])
        self.assertEqual(sent[-1][0], BOSS)
        self.assertEqual(run(50 * HOUR), [])


if __name__ == "__main__":
    unittest.main()
