from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C = _load("contacts_under_test", "platform/core/contacts.py")
transcript = _load("call_transcript_for_contacts", "platform/channels/call_transcript.py")
govern = _load("govern_action_for_contacts", "scripts/govern-action.py")
cli = _load("runtime_contacts_cli", "scripts/runtime-contacts.py")

VISA = "4111 1111 1111 1111"


def interaction(object_id: str, **overrides) -> dict:
    base = {
        "objectId": object_id,
        "interactionType": "call",
        "channel": "voice",
        "direction": "inbound",
        "timestamp": "2026-10-20T10:00:00+08:00",
        "summary": "Asked about boiler servicing; promised a callback on Tuesday.",
        "transcriptRef": "transcript:abc123",
    }
    base.update(overrides)
    return base


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / ".modelgarden" / "contacts"
        self.store = C.ContactStore(self.root)

    def raw(self) -> dict:
        return json.loads(self.store.path.read_text(encoding="utf-8"))


class CreateTests(StoreCase):
    def test_a_contact_is_created_as_asserted_identity_and_stored_privately(self):
        result = self.store.create_contact({"name": "Alex Tan", "phone": "+65 6123 4567", "email": "Alex@Example.test", "notes": "Prefers mornings"}, now=100)
        self.assertRegex(result["objectId"], r"^c-[0-9a-f]{16}$")
        self.assertEqual(result["externalObjectRef"], f"built-in-crm:contact/{result['objectId']}")
        stored = self.raw()["contacts"][result["objectId"]]
        self.assertEqual(stored["identityEvidence"], "asserted")
        self.assertEqual(stored["keys"], {"phone": "6561234567", "email": "alex@example.test"})
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)

    def test_input_is_validated_and_bounded(self):
        for fields, message in (
            ({}, "at least"),
            ({"notes": "only a note"}, "at least a name"),
            ({"name": "A", "ssn": "123"}, "unsupported"),
            ({"phone": "12"}, "7 to 15 digits"),
            ({"email": "not-an-address"}, "valid address"),
            ({"name": "x" * (C.MAX_FIELD + 1)}, "exceeds"),
            ({"name": "  "}, "non-empty"),
            ({"name": 7}, "non-empty"),
            ({"name": "A", "notes": "n" * (C.MAX_NOTES + 1)}, "exceeds"),
        ):
            with self.assertRaisesRegex(C.ContactError, message, msg=str(fields)):
                self.store.create_contact(fields)
        with self.assertRaises(C.ContactError):
            self.store.create_contact(["name"])  # type: ignore[arg-type]
        self.assertFalse(self.store.path.exists())

    def test_card_numbers_in_notes_are_redacted(self):
        result = self.store.create_contact({"name": "A", "notes": f"paid with {VISA}"})
        notes = self.raw()["contacts"][result["objectId"]]["fields"]["notes"]
        self.assertNotIn("4111", notes)
        self.assertIn(C.REDACTED_CARD, notes)

    def test_an_asserted_identity_is_never_merged_and_the_response_does_not_reveal_a_match(self):
        first = self.store.create_contact({"name": "Alex", "phone": "+65 6123 4567"})
        second = self.store.create_contact({"name": "Someone else", "phone": "6561234567"})
        self.assertNotEqual(first["objectId"], second["objectId"])
        self.assertEqual(set(second), {"objectId", "created", "externalObjectRef"})
        self.assertEqual(len(self.raw()["contacts"]), 2)
        review = self.store.review_queue()["possibleDuplicates"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["kind"], "possible-duplicate")
        self.assertEqual(review[0]["contacts"], [second["objectId"], first["objectId"]])

    def test_distinct_people_do_not_enter_the_review_queue(self):
        self.store.create_contact({"name": "A", "phone": "+65 6123 4567"})
        self.store.create_contact({"name": "B", "phone": "+65 6999 0000", "email": "b@example.test"})
        self.assertEqual(self.store.review_queue()["possibleDuplicates"], [])

    def test_capacity_is_bounded(self):
        original = C.MAX_CONTACTS
        C.MAX_CONTACTS = 2
        self.addCleanup(setattr, C, "MAX_CONTACTS", original)
        self.store.create_contact({"name": "A"})
        self.store.create_contact({"name": "B"})
        with self.assertRaisesRegex(C.ContactError, "full"):
            self.store.create_contact({"name": "C"})


class InteractionTests(StoreCase):
    def contact(self) -> str:
        return self.store.create_contact({"name": "Alex Tan", "phone": "+65 6123 4567"})["objectId"]

    def test_filing_is_idempotent_across_retries_and_restarts(self):
        contact = self.contact()
        first = self.store.log_interaction(interaction(contact), now=100)
        self.assertEqual((first["duplicate"], first["matched"]), (False, True))
        self.assertRegex(first["interactionId"], r"^i-[0-9a-f]{24}$")
        again = self.store.log_interaction(interaction(contact), now=999)
        restarted = C.ContactStore(self.root).log_interaction(interaction(contact), now=1000)
        self.assertEqual((again["interactionId"], restarted["interactionId"]), (first["interactionId"], first["interactionId"]))
        self.assertTrue(again["duplicate"] and restarted["duplicate"])
        self.assertEqual(len(self.raw()["interactions"]), 1)
        self.assertEqual(self.raw()["interactions"][first["interactionId"]]["loggedAt"], 100)

    def test_a_different_interaction_gets_its_own_record(self):
        contact = self.contact()
        one = self.store.log_interaction(interaction(contact))
        later = self.store.log_interaction(interaction(contact, timestamp="2026-10-21T10:00:00+08:00"))
        other_channel = self.store.log_interaction(interaction(contact, interactionType="email", channel="support-mailbox", transcriptRef="message:xyz"))
        self.assertEqual(len({one["interactionId"], later["interactionId"], other_channel["interactionId"]}), 3)

    def test_without_a_transcript_reference_the_summary_anchors_the_identity(self):
        contact = self.contact()
        a = self.store.log_interaction(interaction(contact, transcriptRef=None, summary="Wants a quote"))
        b = self.store.log_interaction(interaction(contact, transcriptRef=None, summary="Wants a quote"))
        c = self.store.log_interaction(interaction(contact, transcriptRef=None, summary="Wants a refund"))
        self.assertEqual(a["interactionId"], b["interactionId"])
        self.assertNotEqual(a["interactionId"], c["interactionId"])

    def test_an_unknown_contact_is_an_error_not_a_guess(self):
        with self.assertRaisesRegex(C.ContactError, "unknown contact"):
            self.store.log_interaction(interaction("c-0000000000000000"))
        self.assertFalse(self.store.path.exists())

    def test_unmatched_interactions_wait_for_a_person(self):
        result = self.store.log_interaction(interaction(C.UNMATCHED))
        self.assertFalse(result["matched"])
        queue = self.store.review_queue()
        self.assertEqual([item["id"] for item in queue["unmatchedInteractions"]], [result["interactionId"]])
        contact = self.contact()
        self.store.assign_interaction(result["interactionId"], contact)
        self.assertEqual(self.store.review_queue()["unmatchedInteractions"], [])
        self.assertEqual(self.raw()["interactions"][result["interactionId"]]["contact"], contact)
        with self.assertRaisesRegex(C.ContactError, "no such unmatched"):
            self.store.assign_interaction(result["interactionId"], contact)

    def test_input_is_validated(self):
        contact = self.contact()
        for overrides, message in (
            ({"interactionType": "sms"}, "interactionType"),
            ({"direction": "sideways"}, "direction"),
            ({"timestamp": "yesterday"}, "RFC3339"),
            ({"timestamp": "2026-10-20T10:00:00"}, "UTC offset"),
            ({"summary": ""}, "non-empty"),
            ({"summary": "s" * (C.MAX_SUMMARY + 1)}, "exceeds"),
            ({"channel": ""}, "non-empty"),
            ({"outcome": "o" * (C.MAX_OUTCOME + 1)}, "exceeds"),
            ({"transcriptRef": "the caller said hello"}, "reference"),
            ({"transcriptRef": "x" * 300}, "reference"),
            ({"objectId": ""}, "non-empty"),
        ):
            with self.assertRaisesRegex(C.ContactError, message, msg=str(overrides)):
                self.store.log_interaction(interaction(contact, **overrides))
        self.assertEqual(self.raw()["interactions"], {})

    def test_card_numbers_in_summaries_are_redacted_before_storage(self):
        contact = self.contact()
        result = self.store.log_interaction(interaction(contact, summary=f"Caller read out {VISA} to pay"))
        stored = self.raw()["interactions"][result["interactionId"]]["summary"]
        self.assertNotIn("4111", stored)
        self.assertIn(C.REDACTED_CARD, stored)
        self.assertNotIn("4111", self.store.path.read_text(encoding="utf-8"))

    def test_capacity_is_bounded(self):
        original = C.MAX_INTERACTIONS
        C.MAX_INTERACTIONS = 1
        self.addCleanup(setattr, C, "MAX_INTERACTIONS", original)
        contact = self.contact()
        self.store.log_interaction(interaction(contact))
        with self.assertRaisesRegex(C.ContactError, "full"):
            self.store.log_interaction(interaction(contact, timestamp="2026-10-22T10:00:00+08:00"))
        # A retry of one already stored still succeeds as a duplicate.
        self.assertTrue(self.store.log_interaction(interaction(contact))["duplicate"])

    def test_concurrent_retries_file_exactly_one_record(self):
        contact = self.contact()
        results: list[dict] = []

        def worker() -> None:
            results.append(C.ContactStore(self.root).log_interaction(interaction(contact)))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({item["interactionId"] for item in results}), 1)
        self.assertEqual(sum(1 for item in results if not item["duplicate"]), 1)
        self.assertEqual(len(self.raw()["interactions"]), 1)


class ReadTests(StoreCase):
    def seed(self) -> tuple[str, str]:
        alex = self.store.create_contact({"name": "Alex Tan", "phone": "+65 6123 4567", "email": "alex@example.test"})["objectId"]
        sam = self.store.create_contact({"name": "Sam Lee", "phone": "+65 6999 0000"})["objectId"]
        self.store.log_interaction(interaction(alex))
        self.store.log_interaction(interaction(alex, timestamp="2026-10-21T09:00:00+08:00", summary="Followed up"))
        return alex, sam

    def test_only_a_verified_member_gets_records_back(self):
        alex, _ = self.seed()
        for principal in (None, "public", "anything-else"):
            result = self.store.read(object_id=alex, principal_access=principal)
            self.assertEqual(result, {"records": [], "reason": "requires-verified-staff-principal"})
            self.assertEqual(self.store.read(query="Alex", principal_access=principal)["records"], [])

    def test_member_reads_by_id_with_the_latest_interactions_first(self):
        alex, _ = self.seed()
        record = self.store.read(object_id=alex, principal_access="member")["records"][0]
        self.assertEqual((record["objectId"], record["identityEvidence"]), (alex, "asserted"))
        self.assertEqual(record["fields"]["name"], "Alex Tan")
        self.assertEqual([item["summary"] for item in record["interactions"]], ["Followed up", "Asked about boiler servicing; promised a callback on Tuesday."])

    def test_member_queries_match_name_phone_suffix_and_email(self):
        alex, sam = self.seed()
        ids = lambda **kw: [r["objectId"] for r in self.store.read(principal_access="member", **kw)["records"]]  # noqa: E731
        self.assertEqual(ids(query="alex"), [alex])
        self.assertEqual(ids(query="Lee"), [sam])
        self.assertEqual(ids(query="+65 6123 4567"), [alex])
        self.assertEqual(ids(query="4567"), [alex])
        self.assertEqual(ids(query="ALEX@example.test"), [alex])
        self.assertEqual(ids(query="nobody"), [])
        self.assertEqual(self.store.read(query="nobody", principal_access="member")["reason"], "no-matching-record")
        self.assertEqual(ids(query="a", limit=1), [alex])  # limit clamps; "a" matches both names
        self.assertEqual(len(ids(query="a", limit=99)), 2)

    def test_short_digit_fragments_do_not_match_phone_numbers(self):
        alex, _ = self.seed()
        self.assertEqual(self.store.read(query="123", principal_access="member")["records"], [])

    def test_read_needs_an_id_or_a_query_and_bounds_the_query(self):
        with self.assertRaisesRegex(C.ContactError, "objectId or a query"):
            self.store.read(principal_access="member")
        with self.assertRaisesRegex(C.ContactError, "exceeds"):
            self.store.read(query="q" * (C.MAX_QUERY + 1), principal_access="member")

    def test_unmatched_queue_is_readable_by_members(self):
        self.store.log_interaction(interaction(C.UNMATCHED))
        record = self.store.read(object_id=C.UNMATCHED, principal_access="member")["records"][0]
        self.assertEqual(record["objectId"], C.UNMATCHED)
        self.assertEqual(len(record["interactions"]), 1)


class ReviewAndErasureTests(StoreCase):
    def test_erasing_a_contact_removes_its_interactions_and_review_items(self):
        first = self.store.create_contact({"name": "A", "phone": "+65 6123 4567"})["objectId"]
        second = self.store.create_contact({"name": "B", "phone": "+65 6123 4567"})["objectId"]
        self.store.log_interaction(interaction(first))
        self.store.log_interaction(interaction(second))
        counts = self.store.erase_contact(second)
        self.assertEqual(counts, {"contacts": 1, "interactions": 1, "reviewItems": 1})
        state = self.raw()
        self.assertEqual(list(state["contacts"]), [first])
        self.assertEqual(len(state["interactions"]), 1)
        self.assertEqual(state["review"], [])
        with self.assertRaisesRegex(C.ContactError, "unknown contact"):
            self.store.erase_contact(second)

    def test_erase_interaction_dismiss_review_and_unknown_ids(self):
        first = self.store.create_contact({"name": "A", "phone": "+65 6123 4567"})["objectId"]
        self.store.create_contact({"name": "B", "phone": "+65 6123 4567"})
        filed = self.store.log_interaction(interaction(first))
        self.assertEqual(self.store.erase_interaction(filed["interactionId"]), {"interactions": 1})
        with self.assertRaisesRegex(C.ContactError, "unknown interaction"):
            self.store.erase_interaction(filed["interactionId"])
        review_id = self.store.review_queue()["possibleDuplicates"][0]["id"]
        self.store.dismiss_review(review_id)
        with self.assertRaisesRegex(C.ContactError, "no such review"):
            self.store.dismiss_review(review_id)
        with self.assertRaisesRegex(C.ContactError, "no such unmatched"):
            self.store.assign_interaction("i-" + "0" * 24, first)
        waiting = self.store.log_interaction(interaction(C.UNMATCHED))["interactionId"]
        with self.assertRaisesRegex(C.ContactError, "unknown contact"):
            self.store.assign_interaction(waiting, "c-" + "0" * 16)

    def test_a_corrupt_or_foreign_store_is_refused_not_overwritten(self):
        self.root.mkdir(parents=True)
        self.store.path.write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
        with self.assertRaisesRegex(C.ContactError, "unsupported format"):
            self.store.create_contact({"name": "A"})
        self.assertEqual(json.loads(self.store.path.read_text())["schemaVersion"], 99)


class OperatorScriptTests(StoreCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_review_assign_dismiss_and_erase_are_ledgered_without_personal_data(self):
        audit = self.root.parent / "audit.jsonl"
        a = self.store.create_contact({"name": "Alex Tan", "phone": "+65 6123 4567"})["objectId"]
        b = self.store.create_contact({"name": "Alex T", "phone": "+65 6123 4567"})["objectId"]
        unmatched = self.store.log_interaction(interaction(C.UNMATCHED))["interactionId"]
        code, out, _ = self.run_cli("review")
        queue = json.loads(out)
        self.assertEqual((code, len(queue["possibleDuplicates"]), len(queue["unmatchedInteractions"])), (0, 1, 1))
        self.assertEqual(self.run_cli("assign", unmatched, a, "--by", "jordan@client.example")[0], 0)
        review_id = queue["possibleDuplicates"][0]["id"]
        self.assertEqual(self.run_cli("dismiss", review_id, "--by", "jordan@client.example")[0], 0)
        code, out, _ = self.run_cli("erase-contact", b, "--by", "jordan@client.example")
        self.assertEqual((code, json.loads(out)["contacts"]), (0, 1))
        events = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual([event["action"] for event in events], ["contacts.assign", "contacts.dismiss", "contacts.erase-contact"])
        self.assertTrue(all(event["performedBy"] == "jordan@client.example" and event["event"] == "operator-action" for event in events))
        text = audit.read_text()
        for secret in ("Alex", "6123", "callback"):
            self.assertNotIn(secret, text)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])

    def test_missing_identity_and_unknown_ids_are_errors(self):
        code, _, err = self.run_cli("erase-contact", "c-" + "0" * 16, "--by", "jordan")
        self.assertEqual(code, 1)
        self.assertIn("unknown contact", err)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli("erase-contact", "c-" + "0" * 16)  # --by is required

    def test_ledgering_requires_a_named_person(self):
        with self.assertRaises(ValueError):
            govern.record_operator_action(None, "contacts.erase-contact", " ")
        with self.assertRaises(ValueError):
            govern.record_operator_action(None, "", "jordan")


class ParityTests(unittest.TestCase):
    def test_card_redaction_matches_the_call_transcript(self):
        samples = [
            f"my card is {VISA} thanks", "4111-1111-1111-1111", "4111111111111111", "4111 1111 1111 1112",
            "call +65 6123 4567", "booking 20261020", "order 12345", f"two cards {VISA} and 5500 0000 0000 0004",
        ]
        for sample in samples:
            expected, _ = transcript.redact(sample)
            actual = C.redact_cards(sample).replace(C.REDACTED_CARD, transcript.CARD_REPLACEMENT)
            self.assertEqual(actual, expected, sample)


if __name__ == "__main__":
    unittest.main()
