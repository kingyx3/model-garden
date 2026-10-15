"""Credential-free checks for owner notices about connections and provider grants."""
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


notices = _load("connection_notices_under_test", "platform/core/connection_notices.py")
grants_module = _load("grants_for_notices", "platform/core/provider_grants.py")
govern = _load("govern_for_notices", "scripts/govern-action.py")
Error = notices.NoticeError
NOW = 1_800_000_000
DAY = 86400
OWNER = "owner@client.example"
SECRET_REASON = "token rotated, new secret is hunter2"
CONNECTORS = {"calendar": {"provider": "google", "tier": 1, "owner": OWNER, "token_ref": "secret://google/dev"},
              "crm": {"provider": "built-in-crm", "tier": 0}}


def health(family="calendar", state="failed", updated="2026-10-15T10:00:00Z", **extra):
    return {family: {"family": family, "health": state, "updatedBy": "operator@platform.example", "updatedAt": updated, "reason": SECRET_REASON, **extra}}


class ConnectionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "governance"
        self.events: list[tuple] = []
        self.store = notices.NoticeStore(self.root, audit=lambda *event: self.events.append(event))

    def due(self, health_map=None, connectors=None, grants=(), **kwargs):
        return self.store.due(health_map if health_map is not None else health(), connectors or CONNECTORS, grants, now=NOW, **kwargs)

    def test_a_failed_connection_notifies_its_named_owner_with_a_recovery_step(self):
        (notice,) = self.due()
        self.assertEqual((notice["kind"], notice["family"], notice["to"], notice["unowned"]), ("connection-failed", "calendar", OWNER, False))
        self.assertIn("calendar connection is failed", notice["text"])
        self.assertIn("reconnect it", notice["text"])

    def test_degraded_and_revoked_are_noticed_and_healthy_is_not(self):
        self.assertEqual([item["kind"] for item in self.due(health("calendar", "degraded"))], ["connection-degraded"])
        self.assertIn("authorise it again", self.due(health("calendar", "revoked", "2026-10-16T00:00:00Z"))[0]["text"])
        self.assertEqual(self.due(health("calendar", "healthy", "2026-10-17T00:00:00Z")), [])

    def test_notices_never_carry_the_reason_or_any_secret(self):
        for state in ("failed", "revoked", "degraded"):
            for item in self.due(health("calendar", state, f"2026-10-1{len(state)}T00:00:00Z")):
                blob = json.dumps(item)
                self.assertNotIn("hunter2", blob)
                self.assertNotIn("secret://", blob)
                self.assertNotIn("token", blob.lower().replace("connection", ""))

    def test_a_notice_is_offered_until_delivery_is_recorded_and_then_not_repeated(self):
        first = self.due()
        again = self.due()
        self.assertEqual([item["key"] for item in first], [item["key"] for item in again])  # at least once
        result = self.store.mark_notified(first[0]["key"], via="slack", now=NOW + 5)
        self.assertEqual((result["via"], result["duplicate"]), ("slack", False))
        self.assertEqual(self.due(), [])  # never repeated for an unchanged state
        self.assertTrue(self.store.mark_notified(first[0]["key"], via="email", now=NOW + 9)["duplicate"])
        self.assertEqual(self.events[0][:2], ("connection.notice-delivered", "runtime:notices"))
        self.assertEqual(len([event for event in self.events]), 1)

    def test_a_recovery_forgets_the_record_so_a_later_failure_is_told_again(self):
        key = self.due()[0]["key"]
        self.store.mark_notified(key, via="slack", now=NOW)
        self.assertEqual(self.due(health("calendar", "healthy", "2026-10-15T12:00:00Z")), [])
        again = self.due(health("calendar", "failed", "2026-10-15T14:00:00Z"))
        self.assertEqual(len(again), 1)
        self.assertNotEqual(again[0]["key"], key)  # a new failure, a new notice

    def test_a_new_condition_on_the_same_connection_is_a_new_notice(self):
        failed = self.due(health("calendar", "failed", "2026-10-15T10:00:00Z"))[0]
        self.store.mark_notified(failed["key"], via="slack", now=NOW)
        revoked = self.due(health("calendar", "revoked", "2026-10-15T11:00:00Z"))
        self.assertEqual([item["kind"] for item in revoked], ["connection-revoked"])

    def test_a_connection_without_a_named_owner_goes_to_the_escalation_contact_or_is_flagged_unowned(self):
        connectors = {"calendar": {"provider": "google", "tier": 1}}
        (escalated,) = self.due(connectors=connectors, escalate_to="ops@platform.example")
        self.assertEqual((escalated["to"], escalated["unowned"]), ("ops@platform.example", True))
        store = notices.NoticeStore(self.root / "other")
        (nobody,) = store.due(health(), connectors, now=NOW)
        self.assertEqual((nobody["to"], nobody["unowned"]), (None, True))  # never silently dropped

    def test_only_an_offered_notice_can_be_marked_and_inputs_are_validated(self):
        for key in ("short", "Z" * 64, None, 5):
            with self.subTest(key=key), self.assertRaises(Error):
                self.store.mark_notified(key, via="slack", now=NOW)
        with self.assertRaisesRegex(Error, "no such notice was offered"):
            self.store.mark_notified("0" * 64, via="slack", now=NOW)
        key = self.due()[0]["key"]
        for via in ("", "has space", None):
            with self.subTest(via=via), self.assertRaises(Error):
                self.store.mark_notified(key, via=via, now=NOW)
        with self.assertRaises(Error):
            self.due(expiry_warning_seconds=-1)

    def test_the_state_survives_a_restart_and_is_private(self):
        key = self.due()[0]["key"]
        self.store.mark_notified(key, via="slack", now=NOW)
        restarted = notices.NoticeStore(self.root)
        self.assertEqual(restarted.due(health(), CONNECTORS, now=NOW), [])
        self.assertEqual((self.root / "connection-notices.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_delivery_is_ledgered_as_identifiers_only(self):
        key = self.due()[0]["key"]
        self.store.mark_notified(key, via="inbox", now=NOW)
        audit = pathlib.Path(self._tmp.name) / "audit.jsonl"
        for action, by, details in self.events:
            govern.record_operator_action(audit, action, by, details)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])
        self.assertNotIn(SECRET_REASON, audit.read_text())


class GrantCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.registry = grants_module.GrantRegistry(pathlib.Path(self._tmp.name) / "grants", tenant="acme-prod")
        self.store = notices.NoticeStore(pathlib.Path(self._tmp.name) / "governance")

    def grant(self, **overrides):
        values = dict(agent="receptionist", family="crm", provider="hubspot", connection_id="hubspot-main", isolation="own-identity",
                      acting_identity="svc-receptionist@acme.example", secret_ref="secret://hubspot/acme-prod/receptionist",
                      scopes=["crm.read"], owner=OWNER, by="operator@platform.example", now=NOW - 100)
        values.update(overrides)
        return self.registry.create(**values)

    def due(self, **kwargs):
        return self.store.due({}, {}, self.registry.list(), now=NOW, **kwargs)

    def test_a_healthy_grant_far_from_expiry_needs_no_notice(self):
        self.grant(expires_at=NOW + 30 * DAY)
        self.grant(agent="researcher", connection_id="hubspot-research", acting_identity="svc-r@acme.example", secret_ref="secret://hubspot/acme-prod/r")
        self.assertEqual(self.due(), [])

    def test_an_expiring_grant_is_flagged_within_the_window_and_an_expired_one_always(self):
        granted = self.grant(expires_at=NOW + 3 * DAY)
        (soon,) = self.due()
        self.assertEqual((soon["kind"], soon["to"], soon["agent"], soon["connectionId"]), ("grant-expiring", OWNER, "receptionist", "hubspot-main"))
        self.assertEqual(self.due(expiry_warning_seconds=DAY), [])  # outside a narrower window
        (soon,) = self.due()  # back inside the window: offered again (a notice that stops applying is forgotten)
        self.store.mark_notified(soon["key"], via="slack", now=NOW)
        later = self.store.due({}, {}, self.registry.list(), now=NOW + 4 * DAY)
        self.assertEqual([item["kind"] for item in later], ["grant-expired"])  # a different condition is a new notice
        self.assertIn("has expired", later[0]["text"])
        self.assertTrue(granted["id"])

    def test_revoked_failed_and_degraded_grants_notify_their_owner(self):
        first = self.grant()
        second = self.grant(agent="researcher", connection_id="hubspot-research", acting_identity="svc-r@acme.example",
                            secret_ref="secret://hubspot/acme-prod/r", owner="second@client.example")
        third = self.grant(agent="analyst", connection_id="hubspot-analysis", acting_identity="svc-a@acme.example",
                           secret_ref="secret://hubspot/acme-prod/a")
        self.registry.revoke(first["id"], by="operator@platform.example", now=NOW - 10)
        self.registry.set_health(second["id"], "failed", by="operator@platform.example")
        self.registry.set_health(third["id"], "degraded", by="operator@platform.example")
        found = {item["kind"]: item for item in self.due()}
        self.assertEqual(sorted(found), ["grant-degraded", "grant-failed", "grant-revoked"])
        self.assertEqual(found["grant-failed"]["to"], "second@client.example")
        self.assertEqual(found["grant-revoked"]["to"], OWNER)
        blob = json.dumps(list(found.values()))
        self.assertNotIn("secret://", blob)
        self.assertNotIn("svc-", blob)  # the acting identity is not repeated in a notice

    def test_a_revoked_grant_is_not_also_reported_as_expired_or_failed(self):
        granted = self.grant(expires_at=NOW + DAY)
        self.registry.revoke(granted["id"], by="operator@platform.example", now=NOW - 10)
        self.assertEqual([item["kind"] for item in self.due()], ["grant-revoked"])

    def test_rotation_clears_a_failed_grant_notice(self):
        granted = self.grant()
        self.registry.set_health(granted["id"], "failed", by="operator@platform.example")
        (failed,) = self.due()
        self.store.mark_notified(failed["key"], via="slack", now=NOW)
        self.registry.rotate(granted["id"], secret_ref="secret://hubspot/acme-prod/receptionist-v2", by="operator@platform.example", now=NOW)
        self.assertEqual(self.due(), [])  # healthy again: nothing to say, and the old record is forgotten


if __name__ == "__main__":
    unittest.main()
