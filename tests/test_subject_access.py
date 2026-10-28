"""Credential-free checks for verified data-subject access and correction requests over the Tier 0 stores (#241)."""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_coworker_pause as paused  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
subject = paused._load("subject_access_under_test", "platform/core/subject_access.py")
contacts = paused._load("contacts_for_subject_access", "platform/core/contacts.py")
conversations = paused._load("conversations_for_subject_access", "platform/core/conversations.py")
NOW = 1_800_000_000
PEOPLE = {"verified_by": "jordan@acme.example", "approved_by": "sam@acme.example", "method": "email-link", "reason": "access request by email"}
THIRD_PARTY = "Alex mentioned her sister Priya, priya.n@other.example, +65 9123 4567"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.events = []
        self.contacts = contacts.ContactStore(self.root / "contacts")
        self.conversations = conversations.ConversationStore(self.root / "conversations", tenant="acme")
        self.service = subject.SubjectAccess(self.root / "subject-access", tenant="acme", contacts=self.contacts, conversations=self.conversations,
                                             audit=lambda action, by, details: self.events.append((action, by, details)))
        self.bundles = self.root / "subject-access" / "bundles"
        self.alex = self.contacts.create_contact({"name": "Alex Tan", "phone": "+65 6123 4567", "email": "alex@acme-customer.example"})["objectId"]
        self.contacts.log_interaction({"objectId": self.alex, "interactionType": "call", "channel": "voice", "direction": "inbound",
                                       "timestamp": "2026-10-20T10:00:00+08:00", "summary": THIRD_PARTY})
        opened = self.conversations.open("chat", "T1/C1", agent="receptionist", now=NOW - 100)
        self.conversations.add_interaction(opened["id"], kind="message", external_id="m1", at=NOW - 90, ref="message:m-1")
        self.conversations.link(opened["id"], self.alex, by="jordan@acme.example", now=NOW - 80)

    def export(self, **who):
        return self.service.export(self.bundles, now=NOW, **{**PEOPLE, **(who or {"contact_id": self.alex})})


class ExportTests(Case):
    def test_an_export_holds_the_subject_masks_third_parties_and_states_its_exclusions(self):
        result = self.export()
        bundle = json.loads(pathlib.Path(result["path"]).read_text())
        self.assertEqual((bundle["subject"]["id"], bundle["subject"]["fields"]["name"]), (self.alex, "Alex Tan"))
        self.assertEqual((len(bundle["interactions"]), len(bundle["conversations"])), (1, 1))
        text = bundle["interactions"][0]["summary"]
        for private in ("priya.n@other.example", "9123", "4567"):
            self.assertNotIn(private, text)
        self.assertIn("[email]", text)
        self.assertEqual(bundle["expiresAt"], NOW + 7 * 86_400)
        self.assertIn("agent memory", " ".join(bundle["exclusions"]))
        self.assertEqual((bundle["verifiedBy"], bundle["approvedBy"]), ("jordan@acme.example", "sam@acme.example"))
        self.assertEqual(oct(pathlib.Path(result["path"]).stat().st_mode & 0o777), "0o600")

    def assert_no_subject_values_in_audit(self, details):
        # Audit fields are an explicit allowlist: a new free-text field must
        # fail this check, while opaque IDs/digests may contain phone-like hex.
        self.assertEqual(set(details), {"requestId", "contact", "verifiedBy", "approvedBy", "method",
                                        "interactions", "conversations", "digest", "expiresAt"})
        self.assertRegex(details["requestId"], r"^sa-[0-9a-f]{12}$")
        self.assertRegex(details["contact"], r"^c-[0-9a-f]{16}$")
        self.assertRegex(details["digest"], r"^[0-9a-f]{16}$")
        public = {key: value for key, value in details.items()
                  if key not in ("requestId", "contact", "digest")}
        for private in ("Alex", "6123", "acme-customer", "Priya", "priya"):
            self.assertNotIn(private, json.dumps(public))

    def test_the_ledger_records_people_counts_and_a_digest_never_a_value(self):
        self.export()
        action, by, details = self.events[-1]
        self.assertEqual((action, by), ("subject-access.exported", "sam@acme.example"))
        self.assertEqual((details["interactions"], details["conversations"], details["verifiedBy"]), (1, 1, "jordan@acme.example"))
        self.assert_no_subject_values_in_audit(details)
        bundle = self.bundles / f"{details['requestId']}.json"
        self.assertEqual(details["digest"], hashlib.sha256(bundle.read_bytes()).hexdigest()[:16])

    def test_a_hash_collision_with_synthetic_phone_digits_is_not_a_false_privacy_failure(self):
        self.export()
        details = self.events[-1][2]
        # Reproduce main CI's random 6123-in-digest failure deterministically.
        self.assert_no_subject_values_in_audit({**details, "digest": "0000000000006123"})
        for leak in ({"method": "6123"}, {"summary": THIRD_PARTY}, {"contact": "Alex Tan"}):
            with self.subTest(leak=next(iter(leak))):
                with self.assertRaises(AssertionError):
                    self.assert_no_subject_values_in_audit({**details, **leak})

    def test_the_customer_is_found_by_one_exact_id_email_or_phone_and_nothing_else(self):
        for who in ({"email": "ALEX@acme-customer.example"}, {"phone": "65 6123 4567"}, {"contact_id": self.alex}):
            self.assertEqual(self.service.export(self.bundles, now=NOW, **PEOPLE, **who)["interactions"], 1)
        for who, message in (({"email": "nobody@acme-customer.example"}, "no contact matches"), ({}, "exactly one"),
                             ({"contact_id": self.alex, "email": "alex@acme-customer.example"}, "exactly one"), ({"contact_id": "c-nothex"}, "looks like")):
            with self.assertRaisesRegex(subject.SubjectAccessError, message):
                self.service.export(self.bundles, now=NOW, **PEOPLE, **who)

    def test_two_contacts_with_one_phone_are_never_merged_or_guessed_between(self):
        self.contacts.create_contact({"name": "Alexa Tan", "phone": "+65 6123 4567"})
        with self.assertRaisesRegex(subject.SubjectAccessError, "2 contacts match"):
            self.service.export(self.bundles, now=NOW, **PEOPLE, phone="+65 6123 4567")
        self.assertEqual([e for e in self.events if e[0] == "subject-access.exported"], [])
        self.assertFalse(self.bundles.exists() and list(self.bundles.glob("*.json")))

    def test_verifier_and_approver_must_differ_and_the_method_and_reason_are_required(self):
        for bad in ({"approved_by": "jordan@acme.example"}, {"approved_by": "JORDAN@acme.example"}, {"method": "trust-me"}, {"reason": " "}, {"verified_by": "rm -rf"}):
            with self.assertRaises(subject.SubjectAccessError, msg=str(bad)):
                self.export(contact_id=self.alex, **bad)
        self.assertEqual(self.events, [])

    def test_a_retry_is_the_same_request_and_is_ledgered_once_and_expiry_purges_the_bundle(self):
        first, again = self.export(), self.export()
        self.assertEqual((first["requestId"], again["repeat"]), (again["requestId"], True))
        self.assertEqual(len([e for e in self.events if e[0] == "subject-access.exported"]), 1)
        path = pathlib.Path(first["path"])
        self.assertEqual(self.service.purge_expired(self.bundles, now=NOW + 6 * 86_400), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.service.purge_expired(self.bundles, now=NOW + 8 * 86_400), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.events[-1][2], {"bundles": 1})

    def test_after_erasure_nothing_is_exported_and_tenants_do_not_see_each_other(self):
        other = subject.SubjectAccess(self.root / "other", tenant="beta", contacts=contacts.ContactStore(self.root / "contacts-beta"))
        with self.assertRaisesRegex(subject.SubjectAccessError, "no contact matches"):
            other.export(self.bundles, now=NOW, **PEOPLE, contact_id=self.alex)
        self.contacts.erase_contact(self.alex)
        with self.assertRaisesRegex(subject.SubjectAccessError, "no contact matches"):
            self.export()

    def test_a_store_with_no_conversations_still_exports_the_contact(self):
        bare = subject.SubjectAccess(self.root / "bare", tenant="acme", contacts=self.contacts)
        self.assertEqual(bare.export(bare.root / "bundles", now=NOW, **PEOPLE, contact_id=self.alex)["conversations"], 0)



class ExportIntegrityTests(Case):
    """Access bundles are sensitive; a retry must not rewrite an already-approved snapshot."""

    def test_retry_after_contact_changes_never_rewrites_an_unlogged_export(self):
        first = self.export()
        original = pathlib.Path(first["path"]).read_bytes()
        self.contacts.log_interaction({"objectId": self.alex, "interactionType": "call", "channel": "voice",
                                       "direction": "inbound", "timestamp": "2026-10-21T10:00:00+08:00",
                                       "summary": "A new private conversation"})
        with self.assertRaisesRegex(subject.SubjectAccessError, "changed|new approval"):
            self.export()
        self.assertEqual(pathlib.Path(first["path"]).read_bytes(), original)
        self.assertEqual(len([e for e in self.events if e[0] == "subject-access.exported"]), 1)

    def test_registry_symlink_is_refused_without_overwriting_the_target(self):
        self.service.root.mkdir(mode=0o700, parents=True)
        outside = self.root / "outside.json"
        outside.write_text("do not touch", encoding="utf-8")
        self.service.registry.symlink_to(outside)
        with self.assertRaises(subject.SubjectAccessError):
            self.export()
        self.assertEqual(outside.read_text(), "do not touch")

    def test_existing_bundle_symlink_and_hardlink_are_refused(self):
        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack):
                self.bundles.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Compute the deterministic export id without writing a bundle.
                request_id = "sa-" + hashlib.sha256(
                    f"acme|{self.alex}|{PEOPLE['verified_by']}|{PEOPLE['approved_by']}|{PEOPLE['reason']}".encode()
                ).hexdigest()[:12]
                path = self.bundles / f"{request_id}.json"
                outside = self.root / "outside.json"
                outside.write_text("do not touch", encoding="utf-8")
                if attack == "symlink":
                    path.symlink_to(outside)
                else:
                    path.hardlink_to(outside)
                with self.assertRaises(subject.SubjectAccessError):
                    self.export()
                self.assertEqual(outside.read_text(), "do not touch")
                path.unlink()

    def test_untrusted_bundle_directory_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.bundles.parent.mkdir(mode=0o700)
        self.bundles.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(subject.SubjectAccessError):
            self.export()
        self.assertEqual(list(outside.iterdir()), [])

    def test_expiry_purge_rejects_a_forged_registry_path_outside_the_bundle_directory(self):
        first = self.export()
        state = json.loads(self.service.registry.read_text())
        state["exports"][first["requestId"]]["path"] = "../../outside.json"
        self.service.registry.write_text(json.dumps(state), encoding="utf-8")
        outside = self.root / "outside.json"
        outside.write_text("do not touch", encoding="utf-8")
        with self.assertRaises(subject.SubjectAccessError):
            self.service.purge_expired(self.bundles, now=NOW + 8 * 86_400)
        self.assertEqual(outside.read_text(), "do not touch")

    def test_deleting_initialized_registry_never_resets_export_approval_history(self):
        self.export()
        self.service.registry.unlink()
        with self.assertRaises(subject.SubjectAccessError):
            self.export()

    def test_corrupt_registry_fails_closed_instead_of_resetting_approval_history(self):
        self.service.root.mkdir(parents=True, mode=0o700)
        self.service.registry.write_text('{"schemaVersion":1,"exports":[],"corrections":{}}', encoding="utf-8")
        self.service.registry.chmod(0o600)
        with self.assertRaises(subject.SubjectAccessError):
            self.export()


class CorrectionTests(Case):
    def request(self, **extra):
        return self.service.request_correction(contact_id=self.alex, field="phone", now=NOW, **{**{k: v for k, v in PEOPLE.items()}, **extra})

    def test_a_correction_is_recorded_idempotently_resolved_once_and_never_ledgers_a_value(self):
        first = self.request()
        self.assertEqual((first["status"], first["field"], first["repeat"]), ("open", "phone", False))
        self.assertTrue(self.request()["repeat"])
        self.assertEqual(len([e for e in self.events if e[0] == "subject-access.correction-requested"]), 1)
        self.assertEqual([c["requestId"] for c in self.service.corrections(status="open")], [first["requestId"]])
        done = self.service.resolve_correction(first["requestId"], "applied", by="jordan@acme.example", now=NOW + 5)
        self.assertEqual((done["status"], done["resolvedBy"]), ("applied", "jordan@acme.example"))
        with self.assertRaisesRegex(subject.SubjectAccessError, "already applied"):
            self.service.resolve_correction(first["requestId"], "declined", by="jordan@acme.example", now=NOW + 6)
        self.assertEqual(self.service.corrections(status="open"), [])
        for action, _, details in self.events:
            self.assertIn(action, ("subject-access.correction-requested", "subject-access.correction-resolved"))
            self.assertRegex(details["requestId"], r"^sa-[0-9a-f]{12}$")
            self.assertRegex(details["contact"], r"^c-[0-9a-f]{16}$")
            self.assertEqual(set(details), {"requestId", "contact", "field",
                                            "verifiedBy" if action.endswith("requested") else "status"})
            visible = {key: value for key, value in details.items() if key not in ("requestId", "contact")}
            for private in ("6123", "Alex"):
                self.assertNotIn(private, json.dumps(visible))

    def test_bad_requests_are_refused(self):
        for bad in ({"field": "ssn"}, {"approved_by": "jordan@acme.example"}):
            with self.assertRaises(subject.SubjectAccessError, msg=str(bad)):
                self.service.request_correction(contact_id=self.alex, now=NOW, **{**{"field": "phone"}, **PEOPLE, **bad})
        with self.assertRaises(subject.SubjectAccessError):
            self.service.request_correction(contact_id="c-0000000000000000", field="phone", now=NOW, **PEOPLE)
        with self.assertRaises(subject.SubjectAccessError):
            self.service.resolve_correction("sa-000000000000", "applied", by="a", now=NOW)
        with self.assertRaises(subject.SubjectAccessError):
            self.service.resolve_correction("sa-000000000000", "maybe", by="a", now=NOW)


class CliTests(Case):
    def run_script(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/runtime-subject-access.py"), str(self.root), "--tenant", "acme",
                               "--audit-path", str(self.root / "audit.jsonl"), *args], capture_output=True, text=True)

    def test_the_operator_script_exports_ledgers_and_refuses_one_person_doing_both_roles(self):
        done = self.run_script("export", "--contact", self.alex, "--verified-by", "jordan@acme.example", "--method", "call-back",
                               "--approved-by", "sam@acme.example", "--reason", "phone request")
        self.assertEqual(done.returncode, 0, done.stderr)
        path = pathlib.Path(json.loads(done.stdout)["path"])
        self.assertEqual(json.loads(path.read_text())["subject"]["id"], self.alex)
        self.assertEqual([json.loads(line)["action"] for line in (self.root / "audit.jsonl").read_text().splitlines()], ["subject-access.exported"])
        same = self.run_script("export", "--contact", self.alex, "--verified-by", "jordan@acme.example", "--method", "call-back",
                               "--approved-by", "jordan@acme.example", "--reason", "x")
        self.assertEqual(same.returncode, 2)
        self.assertIn("different people", same.stderr)
        listed = self.run_script("list-corrections")
        self.assertEqual(json.loads(listed.stdout), {"corrections": []})


if __name__ == "__main__":
    unittest.main()
