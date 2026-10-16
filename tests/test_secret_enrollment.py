"""Credential-free checks for one-time, owner-only secret entry: the value reaches the vault and nowhere else (#159, #86)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("secret_enrollment_under_test", ROOT / "platform/core/secret_enrollment.py")
enroll = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["secret_enrollment_under_test"] = enroll
spec.loader.exec_module(enroll)
Error = enroll.EnrollmentError
T0 = 1_800_000_000
OWNER, ADMIN, OTHER = "owner@client.example", "admin@client.example", "mallory@client.example"
SECRET = "sk-live-VERY-SECRET-VALUE-9876"
SECOND = "sk-live-SECOND-SECRET-VALUE-5555"


class FakeVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail_put = False
        self.fail_delete = False

    def put(self, ref, value):
        if self.fail_put:
            raise ConnectionError("vault down: " + value)  # a careless vault error that quotes the value
        self.values[ref] = value

    def delete(self, ref):
        if self.fail_delete:
            raise ConnectionError("vault down")
        self.values.pop(ref, None)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = FakeVault()
        self.events: list[tuple] = []
        self.store = enroll.EnrollmentStore(pathlib.Path(self._tmp.name), tenant="acme-prod", vault=self.vault, audit=lambda *event: self.events.append(event))

    def open(self, **overrides):
        values = dict(agent="receptionist", connection_id="crm-front", kind="bearer", owner=OWNER, requested_by=ADMIN, now=T0)
        values.update(overrides)
        return self.store.create(**values)

    def leaks(self, *needles):
        text = self.store.path.read_text() + json.dumps(self.events)
        return [needle for needle in needles if needle in text]


class OpenTests(Case):
    def test_opening_returns_a_token_once_and_stores_only_its_digest(self):
        opened = self.open()
        self.assertEqual((opened["version"], opened["expiresAt"], opened["kind"]), (1, T0 + 900, "bearer"))
        self.assertGreaterEqual(len(opened["token"]), 32)
        self.assertEqual(self.leaks(opened["token"]), [])
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.store.path.parent.stat().st_mode & 0o777), "0o700")
        self.assertNotEqual(self.open()["token"], opened["token"])

    def test_bad_requests_are_refused(self):
        for overrides in ({"agent": ""}, {"agent": "a b"}, {"connection_id": "../x"}, {"kind": "password"}, {"owner": "not valid!"}, {"requested_by": ""},
                          {"ttl_seconds": 30}, {"ttl_seconds": 3601}, {"ttl_seconds": True}, {"ttl_seconds": "900"}):
            with self.assertRaises(Error, msg=str(overrides)):
                self.open(**overrides)
        self.assertEqual(self.events, [])

    def test_a_new_enrollment_for_the_same_connection_cancels_the_old_link(self):
        first = self.open()
        second = self.open(now=T0 + 10)
        with self.assertRaises(Error):
            self.store.submit(first["token"], SECRET, submitted_by=OWNER, now=T0 + 20)
        self.assertEqual(self.store.submit(second["token"], SECRET, submitted_by=OWNER, now=T0 + 20)["version"], 1)

    def test_the_number_of_open_enrollments_is_bounded_and_purge_clears_dead_ones(self):
        for number in range(enroll.MAX_LIVE):
            self.open(connection_id=f"conn-{number}")
        with self.assertRaises(Error):
            self.open(connection_id="one-too-many")
        self.assertEqual(self.store.purge_expired(now=T0 + 2000), enroll.MAX_LIVE)
        self.open(connection_id="fresh", now=T0 + 2000)

    def test_cancelling_an_open_enrollment_kills_the_link(self):
        opened = self.open()
        self.store.cancel(opened["enrollmentId"], by=ADMIN)
        with self.assertRaises(Error):
            self.store.submit(opened["token"], SECRET, submitted_by=OWNER, now=T0 + 1)
        with self.assertRaises(Error):
            self.store.cancel(opened["enrollmentId"], by=ADMIN)


class SubmitTests(Case):
    def test_the_owner_submits_once_and_the_value_reaches_only_the_vault(self):
        opened = self.open()
        result = self.store.submit(opened["token"], SECRET, submitted_by=OWNER, now=T0 + 60, credential_expires_at=T0 + 90 * 86400)
        self.assertEqual(result["ref"], "secret://acme-prod/receptionist/crm-front/v1")
        self.assertEqual(self.vault.values, {result["ref"]: SECRET})
        self.assertEqual(result["masked"], f"set ({len(SECRET)} characters)")
        self.assertEqual(self.leaks(SECRET, "9876", "VERY-SECRET"), [])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual([event[0] for event in self.events], ["secret.enrollment-opened", "secret.set"])

    def test_the_link_works_once(self):
        opened = self.open()
        self.store.submit(opened["token"], SECRET, submitted_by=OWNER, now=T0 + 60)
        with self.assertRaisesRegex(Error, "not valid"):
            self.store.submit(opened["token"], SECOND, submitted_by=OWNER, now=T0 + 61)
        self.assertEqual(list(self.vault.values.values()), [SECRET])
        self.assertEqual(self.events[-1][2]["reason"], "used")

    def test_an_expired_link_a_wrong_person_and_a_guessed_token_are_refused_without_writing_anything(self):
        for use_real_token, person, now, reason, token in ((True, OWNER, T0 + 901, "expired", None), (True, OTHER, T0 + 5, "wrong-person", None),
                                                          (False, OWNER, T0 + 5, "unknown-token", "A" * 43), (False, OWNER, T0 + 5, "malformed-token", "short"),
                                                          (False, OWNER, T0 + 5, "malformed-token", None)):
            opened = self.open()
            with self.assertRaisesRegex(Error, "not valid", msg=reason):
                self.store.submit(opened["token"] if use_real_token else token, SECRET, submitted_by=person, now=now)  # type: ignore[arg-type]
            self.assertEqual(self.events[-1][2]["reason"], reason)
            self.assertEqual(self.leaks(SECRET, opened["token"]), [])
        self.assertEqual(self.vault.values, {})

    def test_a_wrong_person_does_not_burn_the_link_for_the_owner(self):
        opened = self.open()
        with self.assertRaises(Error):
            self.store.submit(opened["token"], SECRET, submitted_by=OTHER, now=T0 + 5)
        self.assertEqual(self.store.submit(opened["token"], SECRET, submitted_by=OWNER, now=T0 + 6)["version"], 1)

    def test_values_are_validated_by_kind_and_nothing_is_stored_on_refusal(self):
        for kind, value in (("bearer", ""), ("bearer", "two\nlines"), ("bearer", " padded"), ("bearer", "x" * 4097), ("bearer", "tab\there"), ("bearer", 5),
                            ("basic", "nocolon"), ("basic", ":nouser"), ("api-key", "a\x00b")):
            opened = self.open(kind=kind)
            with self.assertRaises(Error, msg=repr((kind, value))):
                self.store.submit(opened["token"], value, submitted_by=OWNER, now=T0 + 1)  # type: ignore[arg-type]
            self.assertEqual(self.vault.values, {})
        opened = self.open(kind="basic")
        self.store.submit(opened["token"], "svc-user:s3cret", submitted_by=OWNER, now=T0 + 1)

    def test_a_vault_that_refuses_leaves_the_link_usable_and_leaks_nothing(self):
        opened = self.open()
        self.vault.fail_put = True
        with self.assertRaisesRegex(Error, "could not be stored") as raised:
            self.store.submit(opened["token"], SECRET, submitted_by=OWNER, now=T0 + 1)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertEqual(self.leaks(SECRET), [])
        self.assertEqual(self.events[-1][2]["error"], "ConnectionError")
        self.vault.fail_put = False
        self.store.submit(opened["token"], SECRET, submitted_by=OWNER, now=T0 + 2)
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0 + 3)["state"], "set")

    def test_a_credential_expiry_must_be_in_the_future(self):
        opened = self.open()
        for expiry in (T0, T0 - 5, True, "soon", 1.5):
            with self.assertRaises(Error, msg=str(expiry)):
                self.store.submit(opened["token"], SECRET, submitted_by=OWNER, now=T0 + 1, credential_expires_at=expiry)  # type: ignore[arg-type]


class LifecycleTests(Case):
    def set_first(self):
        self.store.submit(self.open()["token"], SECRET, submitted_by=OWNER, now=T0 + 1, credential_expires_at=T0 + 30 * 86400)

    def test_status_shows_a_mask_and_expiry_health_but_never_the_value(self):
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0), {"state": "unset"})
        self.set_first()
        status = self.store.status("receptionist", "crm-front", now=T0 + 100)
        self.assertEqual((status["state"], status["health"], status["version"], status["setBy"], status["masked"]), ("set", "ok", 1, OWNER, f"set ({len(SECRET)} characters)"))
        self.assertNotIn(SECRET[:6], json.dumps(status))
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0 + 25 * 86400)["health"], "expiring")
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0 + 31 * 86400)["health"], "expired")

    def test_rotation_writes_the_new_version_first_and_then_removes_the_old_one(self):
        self.set_first()
        opened = self.open(now=T0 + 100)
        self.assertEqual(opened["version"], 2)
        self.assertEqual(list(self.vault.values), ["secret://acme-prod/receptionist/crm-front/v1"])  # opening changes nothing
        result = self.store.submit(opened["token"], SECOND, submitted_by=OWNER, now=T0 + 110)
        self.assertEqual((result["version"], self.vault.values), (2, {"secret://acme-prod/receptionist/crm-front/v2": SECOND}))
        self.assertTrue([event for event in self.events if event[0] == "secret.set"][-1][2]["rotated"])
        self.assertEqual(self.leaks(SECRET, SECOND), [])

    def test_a_failed_rotation_leaves_the_working_credential_in_place(self):
        self.set_first()
        opened = self.open(now=T0 + 100)
        self.vault.fail_put = True
        with self.assertRaises(Error):
            self.store.submit(opened["token"], SECOND, submitted_by=OWNER, now=T0 + 110)
        self.assertEqual(list(self.vault.values.values()), [SECRET])
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0 + 120)["version"], 1)

    def test_a_cleanup_failure_after_rotation_is_reported_not_fatal(self):
        self.set_first()
        opened = self.open(now=T0 + 100)
        self.vault.fail_delete = True
        self.store.submit(opened["token"], SECOND, submitted_by=OWNER, now=T0 + 110)
        self.assertIn("secret.cleanup-failed", [event[0] for event in self.events])
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0 + 120)["version"], 2)

    def test_revoking_deletes_the_credential_and_cancels_open_links(self):
        self.set_first()
        pending = self.open(now=T0 + 100)
        result = self.store.revoke("receptionist", "crm-front", by=ADMIN, now=T0 + 200)
        self.assertEqual((result["state"], self.vault.values), ("revoked", {}))
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0 + 201)["state"], "revoked")
        with self.assertRaises(Error):
            self.store.submit(pending["token"], SECOND, submitted_by=OWNER, now=T0 + 210)
        with self.assertRaises(Error):
            self.store.revoke("receptionist", "crm-front", by=ADMIN, now=T0 + 300)
        self.assertEqual(self.events[-1][0], "secret.enrollment-rejected")

    def test_a_vault_that_will_not_delete_means_it_is_not_revoked(self):
        self.set_first()
        self.vault.fail_delete = True
        with self.assertRaisesRegex(Error, "still stored"):
            self.store.revoke("receptionist", "crm-front", by=ADMIN, now=T0 + 200)
        self.assertEqual(self.store.status("receptionist", "crm-front", now=T0 + 201)["state"], "set")
        self.assertEqual(self.events[-1][0], "secret.revoke-failed")

    def test_two_coworkers_connecting_the_same_system_have_separate_credentials(self):
        self.store.submit(self.open()["token"], SECRET, submitted_by=OWNER, now=T0 + 1)
        self.store.submit(self.open(agent="researcher")["token"], SECOND, submitted_by=OWNER, now=T0 + 2)
        self.assertEqual(set(self.vault.values.values()), {SECRET, SECOND})
        self.store.revoke("receptionist", "crm-front", by=ADMIN, now=T0 + 10)
        self.assertEqual(self.store.status("researcher", "crm-front", now=T0 + 11)["state"], "set")
        self.assertEqual(self.vault.values, {"secret://acme-prod/researcher/crm-front/v1": SECOND})

    def test_the_references_are_secret_uris_a_grant_registry_accepts(self):
        ref = self.store.submit(self.open()["token"], SECRET, submitted_by=OWNER, now=T0 + 1)["ref"]
        spec = importlib.util.spec_from_file_location("grants_for_enrollment", ROOT / "platform/core/provider_grants.py")
        grants = importlib.util.module_from_spec(spec)
        sys.modules["grants_for_enrollment"] = grants
        spec.loader.exec_module(grants)
        registry = grants.GrantRegistry(pathlib.Path(self._tmp.name) / "grants", tenant="acme-prod")
        grant = registry.create(agent="receptionist", family="crm", provider="vendor", connection_id="crm-front", isolation="isolated-grant",
                                acting_identity="grant-receptionist-1", secret_ref=ref, scopes=["crm.read"], owner=OWNER, by=ADMIN, now=T0 + 5)
        self.assertEqual(grant["agent"], "receptionist")

    def test_tenants_are_separate_and_another_tenants_file_is_refused(self):
        self.open()
        other = enroll.EnrollmentStore(pathlib.Path(self._tmp.name), tenant="other-prod", vault=self.vault)
        self.store.path.rename(other.path)
        with self.assertRaises(Error):
            other.status("receptionist", "crm-front", now=T0)
        for bad in ("Acme", "a/b", ""):
            with self.assertRaises(Error):
                enroll.EnrollmentStore(pathlib.Path(self._tmp.name), tenant=bad, vault=self.vault)


if __name__ == "__main__":
    unittest.main()
