"""Credential-free checks for the guided connect step: plan, consent digest, read-only test, own grant, ledger."""
from __future__ import annotations

import importlib.util
import json
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


connect = _load("connect_under_test", "platform/core/connect.py")
grants = _load("grants_for_connect", "platform/core/provider_grants.py")
govern = _load("govern_for_connect", "scripts/govern-action.py")
Error = connect.ConnectError
T0 = 1_800_000_000
OWNER, OPERATOR = "owner@client.example", "operator@platform.example"
SECRET_A, SECRET_B = "secret://google/acme-prod/receptionist", "secret://google/acme-prod/researcher"


def passing():
    return {"ok": True}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple] = []
        # Synthetic upstream approval exists exclusively in this test fixture.
        # The real production registry must continue to refuse unshipped vendors.
        integration = connect._integrations()
        real_plan = integration.plan
        def fake_plan(family, provider, tools):
            if provider != "test-upstream":
                return real_plan(family, provider, tools)
            if family != "calendar" or not tools or not set(tools) <= {"calendar.availability", "calendar.book"}:
                raise ValueError("unsupported Tool selection")
            scopes = {"calendar.availability": "https://www.googleapis.com/auth/calendar.freebusy",
                      "calendar.book": "https://www.googleapis.com/auth/calendar.events"}
            return {"tier": 2, "status": "built-credential-free",
                    "scopes": sorted(scopes[t] for t in tools), "blocker": None}
        patch = mock.patch.object(integration, "plan", side_effect=fake_plan)
        patch.start()
        self.addCleanup(patch.stop)
        self.registry = grants.GrantRegistry(pathlib.Path(self._tmp.name) / "grants", tenant="acme-prod")

    def plan(self, **overrides):
        values = dict(family="calendar", provider="test-upstream", tools=["calendar.availability"], agent="receptionist", owner=OWNER, operator=OPERATOR)
        values.update(overrides)
        return connect.plan(**values)

    def apply(self, proposal, **overrides):
        values = dict(owner_consent=proposal["digest"], connection_test=passing, connection_id="calendar-front", isolation="isolated-grant",
                      acting_identity="grant-receptionist-1", secret_ref=SECRET_A, now=T0, audit=lambda *event: self.events.append(event))
        values.update(overrides)
        return connect.apply(proposal, self.registry, **values)

    # -- the plan -----------------------------------------------------------------------

    def test_a_plan_names_the_coworker_the_exact_scopes_and_carries_a_digest(self):
        plan = self.plan()
        self.assertEqual((plan["connectable"], plan["agent"], plan["tier"], plan["tools"]), (True, "receptionist", 2, ["calendar.availability"]))
        self.assertEqual(plan["scopes"], ["https://www.googleapis.com/auth/calendar.freebusy"])
        self.assertNotIn("calendar.events", json.dumps(plan))
        self.assertEqual(plan["digest"], self.plan()["digest"])  # deterministic
        for override in ({"agent": "other"}, {"owner": "x@y.example"}, {"tools": ["calendar.availability", "calendar.book"]}):
            self.assertNotEqual(self.plan(**override)["digest"], plan["digest"], override)

    def test_both_tools_need_both_scopes_and_no_more(self):
        plan = self.plan(tools=["calendar.book", "calendar.availability"])
        self.assertEqual(plan["scopes"], ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.freebusy"])

    def test_scopes_beyond_what_the_tools_need_are_refused(self):
        with self.assertRaisesRegex(Error, "beyond what the selected Tools need"):
            self.plan(requested_scopes=["https://www.googleapis.com/auth/calendar.freebusy", "https://www.googleapis.com/auth/calendar"])
        self.assertTrue(self.plan(requested_scopes=["https://www.googleapis.com/auth/calendar.freebusy"])["connectable"])

    def test_invalid_people_providers_and_tools_are_refused(self):
        for overrides in ({"agent": ""}, {"agent": "../x"}, {"owner": "pass word"}, {"operator": ""}, {"provider": "nope"},
                          {"tools": ["mail.send"]}, {"tools": []}, {"family": "unknown"}):
            with self.subTest(overrides=overrides), self.assertRaises(Error):
                self.plan(**overrides)

    def test_providers_that_are_not_shipped_or_need_no_credentials_are_not_connectable_and_say_why(self):
        hubspot = self.plan(family="crm", provider="hubspot", tools=["crm.read"])
        self.assertEqual((hubspot["connectable"], hubspot["scopes"]), (False, []))
        self.assertIn("not implemented", hubspot["reason"])
        built_in = self.plan(family="crm", provider="built-in-crm", tools=["crm.read"])
        self.assertFalse(built_in["connectable"])
        self.assertIn("needs no credentials", built_in["reason"])
        for proposal in (hubspot, built_in):
            with self.assertRaises(Error):
                self.apply(proposal)
        self.assertEqual(self.registry.list(), [])

    def test_a_personal_mailbox_needs_an_explicit_exception_and_a_shared_one_does_not(self):
        base = dict(family="mail", provider="gmail", tools=["mail.draft"])
        for mailbox in (None, {}, {"kind": "shared"}, {"kind": "company", "address": "support@acme.example"}, {"kind": "shared", "address": "nobody"}):
            with self.subTest(mailbox=mailbox), self.assertRaises(Error):
                self.plan(**base, mailbox=mailbox)
        with self.assertRaisesRegex(Error, "personal mailbox"):
            self.plan(**base, mailbox={"kind": "personal", "address": "jordan@acme.example"})
        shared = self.plan(**base, mailbox={"kind": "shared", "address": "support@acme.example"})
        delegated = self.plan(**base, mailbox={"kind": "delegated", "address": "support@acme.example"})
        personal = self.plan(**base, mailbox={"kind": "personal", "address": "jordan@acme.example"}, owner_exception="Jordan owns this inbox and asked for it")
        self.assertEqual((shared["mailbox"]["kind"], delegated["mailbox"]["kind"], personal["ownerException"].startswith("Jordan")), ("shared", "delegated", True))
        self.assertNotEqual(shared["digest"], personal["digest"])
        with self.assertRaisesRegex(Error, "only applies to the mail family"):
            self.plan(mailbox={"kind": "shared", "address": "a@b.example"})

    # -- apply: consent, test, own grant, ledger -----------------------------------------

    def test_a_passing_test_records_the_coworkers_own_grant_and_returns_the_binding_fragment(self):
        proposal = self.plan()
        result = self.apply(proposal)
        grant = result["grant"]
        self.assertEqual((grant["agent"], grant["family"], grant["provider"], grant["owner"], grant["scopes"]),
                         ("receptionist", "calendar", "test-upstream", OWNER, ["calendar.availability"]))
        self.assertEqual(result["binding"], {"provider": "test-upstream", "tier": 2, "scopes": ["https://www.googleapis.com/auth/calendar.freebusy"], "owner": OWNER})
        self.assertNotIn("secret", json.dumps(result["binding"]).lower())
        self.assertEqual([item["id"] for item in self.registry.list(agent="receptionist")], [grant["id"]])
        action, by, details = self.events[-1]
        self.assertEqual((action, by, details["owner"], details["operator"], details["consentDigest"]), ("connection.connect", OPERATOR, OWNER, OPERATOR, proposal["digest"]))
        self.assertNotIn("secret://", json.dumps(self.events))

    def test_consent_to_one_plan_cannot_be_used_for_another(self):
        small, large = self.plan(), self.plan(tools=["calendar.availability", "calendar.book"])
        calls = []
        for consent in (small["digest"], "", None, "x" * 64, "0" * 64):
            with self.subTest(consent=consent), self.assertRaisesRegex(Error, "consent does not match"):
                self.apply(large, owner_consent=consent, connection_test=lambda: calls.append(1) or passing())
        self.assertEqual((calls, self.registry.list()), ([], []))  # refused before any test or change
        forged = dict(small, tools=["calendar.availability", "calendar.book"])
        with self.assertRaisesRegex(Error, "consent does not match"):
            self.apply(forged, owner_consent=small["digest"])  # a plan edited after consent no longer matches its digest

    def test_a_failing_test_changes_nothing_and_is_ledgered(self):
        proposal = self.plan()
        for test in (lambda: {"ok": False}, lambda: {}, lambda: None, lambda: (_ for _ in ()).throw(TimeoutError("slow"))):
            with self.assertRaisesRegex(Error, "nothing was changed"):
                self.apply(proposal, connection_test=test)
        self.assertEqual(self.registry.list(), [])
        failures = [event for event in self.events if event[0] == "connection.connect-failed"]
        self.assertEqual(len(failures), 4)
        self.assertEqual({event[2]["reason"] for event in failures}, {"test-failed", "test-error:TimeoutError"})
        self.assertTrue(all(event[1] == OPERATOR and event[2]["owner"] == OWNER for event in failures))

    def test_a_second_coworker_gets_its_own_grant_and_cannot_borrow_the_first(self):
        first = self.apply(self.plan())
        second_plan = self.plan(agent="researcher")
        borrowed = lambda: self.apply(second_plan, connection_id="calendar-research", acting_identity="grant-researcher-1", secret_ref=SECRET_A)  # noqa: E731
        with self.assertRaisesRegex(Error, "grant-refused"):
            borrowed()  # the registry will not give two coworkers one credential
        self.assertEqual(len(self.registry.list()), 1)
        second = self.apply(second_plan, connection_id="calendar-research", acting_identity="grant-researcher-1", secret_ref=SECRET_B)
        self.assertNotEqual(first["grant"]["id"], second["grant"]["id"])
        self.assertEqual({item["agent"] for item in self.registry.list()}, {"receptionist", "researcher"})
        self.assertEqual(self.events[-1][0], "connection.connect")
        self.assertIn("connection.connect-failed", [event[0] for event in self.events])

    def test_disconnect_revokes_one_coworker_only_and_is_ledgered_with_both_people(self):
        first = self.apply(self.plan())
        second = self.apply(self.plan(agent="researcher"), connection_id="calendar-research", acting_identity="grant-researcher-1", secret_ref=SECRET_B)
        revoked = connect.disconnect(self.registry, first["grant"]["id"], operator=OPERATOR, owner=OWNER, now=T0 + 5, audit=lambda *event: self.events.append(event))
        self.assertTrue(revoked["revokedAt"])
        by_agent = {item["agent"]: item for item in self.registry.list()}
        self.assertTrue(by_agent["receptionist"].get("revokedAt"))
        self.assertFalse(by_agent["researcher"].get("revokedAt"))
        self.assertEqual(second["grant"]["id"], by_agent["researcher"]["id"])
        action, by, details = self.events[-1]
        self.assertEqual((action, by, details["owner"], details["operator"]), ("connection.disconnect", OPERATOR, OWNER, OPERATOR))
        with self.assertRaises(Error):
            connect.disconnect(self.registry, "g-missing", operator=OPERATOR, owner=OWNER, now=T0)
        with self.assertRaises(Error):
            connect.disconnect(self.registry, first["grant"]["id"], operator="", owner=OWNER, now=T0)

    def test_events_are_accepted_by_the_real_ledger_and_hold_no_credentials(self):
        self.apply(self.plan())
        with self.assertRaises(Error):
            self.apply(self.plan(agent="analyst"), connection_test=lambda: {"ok": False})
        audit = pathlib.Path(self._tmp.name) / "audit.jsonl"
        for action, by, details in self.events:
            govern.record_operator_action(audit, action, by, details)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])
        text = audit.read_text()
        self.assertNotIn("secret://", text)
        self.assertNotIn("grant-receptionist-1", text)  # the provider-side identity is not repeated in connect events

    # -- binding merge ------------------------------------------------------------------

    def test_the_binding_gains_the_fragment_without_changing_the_input_or_dropping_a_live_token_reference(self):
        original = {"environment": "dev", "connectors": {"calendar": {"provider": "test-upstream", "token_ref": "secret://google/dev", "tier": 2}}}
        fragment = self.apply(self.plan())["binding"]
        merged = connect.merge_binding(original, "calendar", fragment)
        self.assertEqual(merged["connectors"]["calendar"]["owner"], OWNER)
        self.assertEqual(merged["connectors"]["calendar"]["token_ref"], "secret://google/dev")  # still serving the running sidecar
        self.assertEqual(original["connectors"]["calendar"], {"provider": "test-upstream", "token_ref": "secret://google/dev", "tier": 2})
        empty = connect.merge_binding({"environment": "dev"}, "calendar", fragment)
        self.assertEqual(empty["connectors"]["calendar"]["provider"], "test-upstream")

    def test_replacing_a_provider_must_be_explicit_and_drops_the_old_providers_token_reference(self):
        original = {"connectors": {"calendar": {"provider": "microsoft-calendar", "token_ref": "secret://ms/dev"}}}
        fragment = {"provider": "test-upstream", "tier": 2, "scopes": ["s"], "owner": OWNER}
        with self.assertRaisesRegex(Error, "must be explicit"):
            connect.merge_binding(original, "calendar", fragment)
        replaced = connect.merge_binding(original, "calendar", fragment, replace=True)
        self.assertEqual(replaced["connectors"]["calendar"], {"provider": "test-upstream", "tier": 2, "scopes": ["s"], "owner": OWNER})

    def test_a_fragment_can_never_carry_a_secret_and_the_family_is_checked(self):
        for fragment in ({"provider": "x", "token_ref": "secret://x"}, {"provider": "x", "secret": "hunter2"}, {"provider": "x", "password": "p"}):
            with self.subTest(fragment=fragment), self.assertRaisesRegex(Error, "never a secret"):
                connect.merge_binding({}, "calendar", fragment)
        with self.assertRaises(Error):
            connect.merge_binding({}, "Calendar", {"provider": "x"})

    # -- changing a working connection --------------------------------------------------

    def existing_binding(self, **overrides):
        entry = {"provider": "test-upstream", "tier": 2, "scopes": ["https://www.googleapis.com/auth/calendar.freebusy"], "owner": OWNER}
        entry.update(overrides)
        return {"connectors": {"calendar": entry}}

    def test_describe_change_names_exactly_what_would_change(self):
        fragment = {"provider": "test-upstream", "tier": 2, "scopes": ["a", "b"], "owner": "other@client.example"}
        change = connect.describe_change({"provider": "test-upstream", "scopes": ["b", "c"], "owner": OWNER}, fragment)
        self.assertEqual((change["broadened"], change["narrowed"], change["ownerChanged"], change["providerChanged"]), (["a"], ["c"], True, False))
        self.assertTrue(connect.describe_change(None, fragment)["new"])

    def test_widening_scopes_or_changing_owner_of_a_scoped_binding_must_be_explicit(self):
        binding = self.existing_binding()
        wider = {"provider": "test-upstream", "tier": 2, "scopes": ["https://www.googleapis.com/auth/calendar.freebusy", "https://www.googleapis.com/auth/calendar.events"], "owner": OWNER}
        with self.assertRaisesRegex(Error, "widening access must be explicit"):
            connect.merge_binding(binding, "calendar", wider)
        self.assertEqual(connect.merge_binding(binding, "calendar", wider, rescope=True)["connectors"]["calendar"]["scopes"], wider["scopes"])
        other_owner = {**wider, "scopes": binding["connectors"]["calendar"]["scopes"], "owner": "someone-else@client.example"}
        with self.assertRaisesRegex(Error, "change of owner must be explicit"):
            connect.merge_binding(binding, "calendar", other_owner)
        narrower = {**other_owner, "owner": OWNER, "scopes": []}
        self.assertEqual(connect.merge_binding(binding, "calendar", narrower)["connectors"]["calendar"]["scopes"], [])  # narrowing needs no ceremony
        self.assertEqual(binding, self.existing_binding())

    def replace(self, proposal, previous, write, **overrides):
        values = dict(previous_grant_id=previous, write_binding=write, owner_consent=proposal["digest"], connection_test=passing,
                      connection_id="calendar-front-2", isolation="isolated-grant", acting_identity="grant-receptionist-2", secret_ref=SECRET_B,
                      now=T0 + 10, audit=lambda *event: self.events.append(event))
        values.update(overrides)
        return connect.replace(proposal, self.registry, **values)

    def test_replace_connects_the_new_grant_writes_the_binding_and_only_then_revokes_the_old_one(self):
        old = self.apply(self.plan())["grant"]["id"]
        written = []
        result = self.replace(self.plan(), old, written.append)
        self.assertEqual((result["previousRevoked"], len(written)), (True, 1))
        active = {grant["id"] for grant in self.registry.list(agent="receptionist") if not grant.get("revokedAt")}
        self.assertEqual(active, {result["grant"]["id"]})
        replaced = [event for event in self.events if event[0] == "connection.replaced"]
        self.assertEqual(len(replaced), 1)
        self.assertEqual((replaced[0][2]["previousGrantId"], replaced[0][2]["newGrantId"]), (old, result["grant"]["id"]))
        self.assertNotIn(SECRET_B, json.dumps(self.events))

    def test_a_failed_binding_write_revokes_the_new_grant_and_leaves_the_working_one_alone(self):
        old = self.apply(self.plan())["grant"]["id"]

        def failing(_fragment):
            raise OSError("disk full")

        with self.assertRaisesRegex(Error, "previous one is unchanged"):
            self.replace(self.plan(), old, failing)
        grants_now = self.registry.list(agent="receptionist")
        self.assertEqual([grant["id"] for grant in grants_now if not grant.get("revokedAt")], [old])
        rolled = [event for event in self.events if event[0] == "connection.rolled-back"]
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0][2]["reason"], "binding-write:OSError")
        self.assertFalse([event for event in self.events if event[0] == "connection.replaced"])

    def test_a_failed_test_changes_nothing_and_the_binding_is_never_written(self):
        old = self.apply(self.plan())["grant"]["id"]
        written = []
        with self.assertRaisesRegex(Error, "nothing was changed"):
            self.replace(self.plan(), old, written.append, connection_test=lambda: {"ok": False})
        self.assertEqual(written, [])
        self.assertEqual([grant["id"] for grant in self.registry.list(agent="receptionist") if not grant.get("revokedAt")], [old])

    def test_only_an_active_grant_of_the_same_coworker_and_capability_can_be_replaced(self):
        mine = self.apply(self.plan())["grant"]["id"]
        other = self.apply(self.plan(agent="researcher"), connection_id="calendar-research", acting_identity="grant-researcher-1", secret_ref=SECRET_A.replace("receptionist", "researcher"))["grant"]["id"]
        with self.assertRaises(Error):
            self.replace(self.plan(), other, lambda _: None)
        with self.assertRaises(Error):
            self.replace(self.plan(), "g" * 12, lambda _: None)
        self.registry.revoke(mine, by=OPERATOR, now=T0 + 5)
        with self.assertRaises(Error):
            self.replace(self.plan(), mine, lambda _: None)

    def test_a_failed_revocation_of_the_old_grant_is_reported_not_hidden(self):
        old = self.apply(self.plan())["grant"]["id"]
        real = self.registry.revoke

        def flaky(grant_id, **kwargs):
            if grant_id == old:
                raise PermissionError("registry unavailable")
            return real(grant_id, **kwargs)

        self.registry.revoke = flaky  # type: ignore[method-assign]
        result = self.replace(self.plan(), old, lambda _: None)
        self.assertFalse(result["previousRevoked"])
        self.assertFalse([e for e in self.events if e[0] == "connection.replaced"][0][2]["previousRevoked"])


if __name__ == "__main__":
    unittest.main()
