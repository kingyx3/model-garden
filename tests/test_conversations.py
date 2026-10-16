"""Credential-free checks for cross-channel conversation timelines and verified customer linkage (#115)."""
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


conversations = _load("conversations_under_test", "platform/core/conversations.py")
Error = conversations.ConversationError
T0 = 1_800_000_000
ADMIN = "owner@client.example"
ALEX_EMAIL, ALEX_PHONE = "Alex.Tan@Customer.example", "+65 6123 4567"
SAM_EMAIL = "sam@other.example"
REQUEST_ID = "a" * 64


def make_directory(table: dict[tuple[str, str], list[str]]):
    def directory(kind, digest):
        return table.get((kind, digest), [])
    return directory


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple] = []
        self.store = conversations.ConversationStore(pathlib.Path(self._tmp.name), tenant="acme-prod", audit=lambda *event: self.events.append(event))
        self.alex_email = conversations.evidence_digest("email", ALEX_EMAIL)
        self.alex_phone = conversations.evidence_digest("phone", ALEX_PHONE)
        self.sam_email = conversations.evidence_digest("email", SAM_EMAIL)
        self.directory = make_directory({("email", self.alex_email): ["c-alex"], ("phone", self.alex_phone): ["c-alex"], ("email", self.sam_email): ["c-sam"]})

    def call(self, external="call-1", **kwargs):
        return self.store.open("voice", external, agent="receptionist", now=T0, **kwargs)["id"]

    def mail(self, external="thread-1", **kwargs):
        return self.store.open("email", external, agent="receptionist", now=T0 + 3600, **kwargs)["id"]


class RecordingTests(Case):
    def test_ids_are_stable_per_channel_call_and_tenant_and_opening_twice_changes_nothing(self):
        first = self.store.open("voice", "call-1", agent="receptionist", agent_version="1.2.0", task_id="t-1", trace_id="tr-1", now=T0)
        again = self.store.open("voice", "call-1", agent="receptionist", now=T0 + 99)
        self.assertEqual((first["id"], first["duplicate"], again["id"], again["duplicate"]), (first["id"], False, first["id"], True))
        self.assertEqual((again["startedAt"], again["agentVersion"], again["taskId"], again["traceId"]), (T0, "1.2.0", "t-1", "tr-1"))
        self.assertNotEqual(self.store.conversation_id("email", "call-1"), first["id"])
        other = conversations.ConversationStore(pathlib.Path(self._tmp.name) / "other", tenant="other-prod")
        self.assertNotEqual(other.conversation_id("voice", "call-1"), first["id"])
        self.assertEqual(self.store.stats()["conversations"], 1)
        self.assertNotIn("call-1", json.dumps(self.store.path.read_text()))  # only a digest of the channel's own id is kept

    def test_an_interaction_is_stored_once_wherever_and_however_often_it_arrives(self):
        cid = self.call()
        first = self.store.add_interaction(cid, kind="message", external_id="turn-1", at=T0 + 5, ref="transcript:abc#1", speaker="caller")
        again = self.store.add_interaction(cid, kind="message", external_id="turn-1", at=T0 + 5, ref="transcript:abc#1", speaker="caller")
        self.assertEqual((first["duplicate"], again["duplicate"], again["key"]), (False, True, first["key"]))
        self.assertEqual(len(self.store.conversation_timeline(cid)), 1)

    def test_late_arrivals_sit_in_the_right_place_on_the_timeline(self):
        cid = self.call()
        for external, at in (("t3", T0 + 30), ("t1", T0 + 10), ("t2", T0 + 20), ("t2b", T0 + 20)):
            self.store.add_interaction(cid, kind="message", external_id=external, at=at, ref=f"transcript:abc#{external}")
        self.assertEqual([item["ref"] for item in self.store.conversation_timeline(cid)],
                         ["transcript:abc#t1", "transcript:abc#t2", "transcript:abc#t2b", "transcript:abc#t3"])  # ties keep arrival order

    def test_an_interaction_is_a_reference_never_content(self):
        cid = self.call()
        for bad in ({"ref": "Alex said his boiler is broken"}, {"ref": "transcript:" + "x" * 300}, {"kind": "secret"}, {"label": "x" * 100},
                    {"request_id": "not-a-request-id"}, {"content_digest": "abc"}, {"at": -1}, {"at": 1.5}, {"external_id": "has space"}, {"speaker": "a b!"}):
            values = dict(kind="message", external_id="t1", at=T0, ref="transcript:abc#1")
            values.update(bad)
            with self.assertRaises(Error, msg=str(bad)):
                self.store.add_interaction(cid, **values)
        with self.assertRaises(Error):
            self.store.add_interaction("cv-nope", kind="message", external_id="t1", at=T0, ref="transcript:abc#1")
        self.assertEqual(self.store.conversation_timeline(cid), [])

    def test_every_kind_of_event_can_sit_on_one_timeline_with_its_receipt_ids(self):
        cid = self.call()
        for number, kind in enumerate(conversations.INTERACTION_KINDS):
            self.store.add_interaction(cid, kind=kind, external_id=f"e{number}", at=T0 + number, ref=f"{kind}:r{number}", request_id=REQUEST_ID if kind == "action" else None,
                                       label="booked" if kind == "outcome" else None, content_digest="b" * 64 if kind == "summary" else None)
        rows = self.store.conversation_timeline(cid)
        self.assertEqual([row["kind"] for row in rows], list(conversations.INTERACTION_KINDS))
        self.assertEqual(next(row for row in rows if row["kind"] == "action")["requestId"], REQUEST_ID)

    def test_bounds_are_enforced(self):
        cid = self.call()
        for number in range(conversations.MAX_INTERACTIONS):
            self.store.add_interaction(cid, kind="message", external_id=f"t{number}", at=T0 + number, ref=f"transcript:abc#{number}")
        with self.assertRaises(Error):
            self.store.add_interaction(cid, kind="message", external_id="one-too-many", at=T0, ref="transcript:abc#x")
        for number in range(conversations.MAX_EVIDENCE):
            self.store.add_evidence(cid, "email", f"person{number}@x.example", verified=False, provenance="caller-asserted")
        with self.assertRaises(Error):
            self.store.add_evidence(cid, "email", "extra@x.example", verified=False, provenance="caller-asserted")


class EvidenceTests(Case):
    def test_evidence_is_kept_only_as_a_digest_of_the_normalised_value(self):
        cid = self.call()
        self.store.add_evidence(cid, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        self.store.add_evidence(cid, "phone", "(65) 6123-4567", verified=True, provenance="sip-callerid")
        text = self.store.path.read_text()
        for secret in ("alex.tan", "customer.example", "6123"):
            self.assertNotIn(secret, text.lower())
        self.assertEqual(conversations.evidence_digest("email", " ALEX.TAN@customer.example "), self.alex_email)
        self.assertEqual(conversations.evidence_digest("phone", "0065 6123 4567"), conversations.evidence_digest("phone", "+65-6123-4567"))
        self.assertEqual(conversations.evidence_digest("phone", "(65) 6123-4567"), self.alex_phone)

    def test_a_name_can_never_be_verified_and_bad_evidence_is_refused(self):
        cid = self.call()
        with self.assertRaises(Error):
            self.store.add_evidence(cid, "name", "Alex Tan", verified=True, provenance="caller-asserted")
        for kind, value in (("email", "not an address"), ("phone", "12"), ("email", ""), ("fax", "123456"), ("phone", None)):
            with self.assertRaises(Error, msg=str((kind, value))):
                self.store.add_evidence(cid, kind, value, verified=False, provenance="caller-asserted")  # type: ignore[arg-type]
        with self.assertRaises(Error):
            self.store.add_evidence(cid, "email", ALEX_EMAIL, verified="yes", provenance="x")  # type: ignore[arg-type]

    def test_later_verification_upgrades_but_never_downgrades_the_same_evidence(self):
        cid = self.call()
        self.store.add_evidence(cid, "email", ALEX_EMAIL, verified=False, provenance="caller-asserted")
        self.store.add_evidence(cid, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        self.store.add_evidence(cid, "email", ALEX_EMAIL, verified=False, provenance="caller-asserted")
        self.assertEqual(self.store.resolve(cid, self.directory, now=T0)["state"], "linked")


class LinkingTests(Case):
    def test_verified_evidence_naming_one_customer_links_and_the_timeline_spans_channels(self):
        voice, mail = self.call(), self.mail()
        self.store.add_evidence(voice, "phone", ALEX_PHONE, verified=True, provenance="sip-callerid")
        self.store.add_evidence(mail, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        self.assertEqual(self.store.resolve(voice, self.directory, now=T0)["customerId"], "c-alex")
        self.assertEqual(self.store.resolve(mail, self.directory, now=T0 + 1)["customerId"], "c-alex")
        self.store.add_interaction(mail, kind="message", external_id="m1", at=T0 + 3700, ref="message:m1")
        self.store.add_interaction(voice, kind="call", external_id="c1", at=T0 + 10, ref="transcript:abc")
        self.store.add_interaction(voice, kind="outcome", external_id="o1", at=T0 + 60, ref="outcome:abc", label="booked")
        rows = self.store.customer_timeline("c-alex")
        self.assertEqual([(row["channel"], row["kind"]) for row in rows], [("voice", "call"), ("voice", "outcome"), ("email", "message")])
        self.assertEqual({row["conversationId"] for row in rows}, {voice, mail})
        self.assertEqual(self.store.customer_timeline("c-sam"), [])

    def test_two_public_identities_stay_separate(self):
        alex, sam = self.call("call-a"), self.call("call-s")
        self.store.add_evidence(alex, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        self.store.add_evidence(sam, "email", SAM_EMAIL, verified=True, provenance="email-dkim")
        self.store.resolve(alex, self.directory, now=T0)
        self.store.resolve(sam, self.directory, now=T0)
        self.store.add_interaction(alex, kind="message", external_id="m", at=T0, ref="message:a")
        self.store.add_interaction(sam, kind="message", external_id="m", at=T0, ref="message:s")
        self.assertEqual([row["ref"] for row in self.store.customer_timeline("c-alex")], ["message:a"])
        self.assertEqual([row["ref"] for row in self.store.customer_timeline("c-sam")], ["message:s"])

    def test_verified_evidence_naming_two_customers_goes_to_review_and_links_nobody(self):
        cid = self.call()
        self.store.add_evidence(cid, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        self.store.add_evidence(cid, "email", SAM_EMAIL, verified=True, provenance="email-dkim")
        outcome = self.store.resolve(cid, self.directory, now=T0)
        self.assertEqual((outcome["state"], outcome["reason"]), ("review", "conflicting-evidence"))
        self.assertIsNone(self.store.link_of(cid))
        (item,) = self.store.review_queue()
        self.assertEqual((item["candidates"], item["reason"]), (["c-alex", "c-sam"], "conflicting-evidence"))
        self.assertEqual(self.store.resolve(cid, self.directory, now=T0 + 1)["reviewId"], outcome["reviewId"])  # not queued twice
        self.assertEqual(len(self.store.review_queue()), 1)

    def test_asserted_evidence_never_links_even_when_it_matches_and_goes_to_a_person(self):
        cid = self.call()
        self.store.add_evidence(cid, "phone", ALEX_PHONE, verified=False, provenance="caller-asserted")
        self.store.add_evidence(cid, "name", "Alex Tan", verified=False, provenance="caller-asserted")
        outcome = self.store.resolve(cid, self.directory, now=T0)
        self.assertEqual((outcome["state"], outcome["reason"]), ("review", "asserted-match"))
        self.assertIsNone(self.store.link_of(cid))
        self.assertEqual(self.store.customer_timeline("c-alex"), [])

    def test_asserted_evidence_that_matches_nobody_and_no_evidence_leave_the_conversation_unlinked(self):
        cid = self.call()
        self.assertEqual(self.store.resolve(cid, self.directory, now=T0), {"state": "unlinked"})
        self.store.add_evidence(cid, "email", "stranger@nowhere.example", verified=True, provenance="email-dkim")
        self.store.add_evidence(cid, "name", "Alex Tan", verified=False, provenance="caller-asserted")
        self.assertEqual(self.store.resolve(cid, self.directory, now=T0), {"state": "unlinked"})
        self.assertEqual(self.store.review_queue(), [])

    def test_a_person_confirms_or_dismisses_and_it_is_ledgered_with_their_name(self):
        cid = self.call()
        self.store.add_evidence(cid, "phone", ALEX_PHONE, verified=False, provenance="caller-asserted")
        review = self.store.resolve(cid, self.directory, now=T0)["reviewId"]
        self.store.confirm(review, "c-alex", by=ADMIN, now=T0 + 5)
        self.assertEqual(self.store.link_of(cid)["confidence"], f"confirmed-by:{ADMIN}")
        self.assertEqual(self.store.review_queue(), [])
        self.assertIn(("conversation.link", ADMIN), [(event[0], event[1]) for event in self.events])
        other = self.call("call-2")
        self.store.add_evidence(other, "phone", ALEX_PHONE, verified=False, provenance="caller-asserted")
        review2 = self.store.resolve(other, self.directory, now=T0)["reviewId"]
        self.store.dismiss(review2, by=ADMIN)
        self.assertIsNone(self.store.link_of(other))
        for bad in (lambda: self.store.confirm("rv-nope", "c-alex", by=ADMIN, now=T0), lambda: self.store.confirm(review2, "c-alex", by="bad name!", now=T0)):
            with self.assertRaises(Error):
                bad()

    def test_resolving_again_after_a_link_changes_nothing_and_a_directory_that_misbehaves_links_nobody(self):
        cid = self.call()
        self.store.add_evidence(cid, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        self.store.resolve(cid, self.directory, now=T0)
        again = self.store.resolve(cid, make_directory({}), now=T0 + 9)
        self.assertEqual((again["state"], again["customerId"], again["duplicate"]), ("linked", "c-alex", True))
        fresh = self.call("call-9")
        self.store.add_evidence(fresh, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        with self.assertRaises(RuntimeError):
            self.store.resolve(fresh, lambda kind, digest: (_ for _ in ()).throw(RuntimeError("crm down")), now=T0)
        self.assertIsNone(self.store.link_of(fresh))


class PrivacyTests(Case):
    def linked(self, external="call-1", email=ALEX_EMAIL):
        cid = self.call(external)
        self.store.add_evidence(cid, "email", email, verified=True, provenance="email-dkim")
        self.store.add_interaction(cid, kind="message", external_id="m", at=T0, ref="message:m")
        self.store.resolve(cid, self.directory, now=T0)
        return cid

    def test_erasing_a_customer_removes_every_linked_conversation_and_nothing_else(self):
        a, b = self.linked("call-1"), self.mail()
        self.store.add_evidence(b, "email", ALEX_EMAIL, verified=True, provenance="email-dkim")
        self.store.resolve(b, self.directory, now=T0)
        sam = self.linked("call-s", SAM_EMAIL)
        self.assertEqual(self.store.erase_customer("c-alex", by=ADMIN), {"conversations": 2})
        self.assertEqual(self.store.customer_timeline("c-alex"), [])
        self.assertEqual(self.store.stats(), {"conversations": 1, "linked": 1, "review": 0})
        with self.assertRaises(Error):
            self.store.conversation_timeline(a)
        self.assertEqual(len(self.store.conversation_timeline(sam)), 1)
        self.assertNotIn("alex.tan", json.dumps(self.events).lower())

    def test_erasing_a_conversation_also_clears_its_review_item(self):
        cid = self.call()
        self.store.add_evidence(cid, "phone", ALEX_PHONE, verified=False, provenance="caller-asserted")
        self.store.resolve(cid, self.directory, now=T0)
        self.assertEqual(self.store.erase_conversation(cid, by=ADMIN), {"conversations": 1})
        self.assertEqual((self.store.review_queue(), self.store.stats()["conversations"]), ([], 0))

    def test_retention_purges_old_conversations(self):
        old = self.store.open("voice", "old", agent="receptionist", now=T0 - 400 * 86400)["id"]
        new = self.call()
        self.assertEqual(self.store.purge_before(T0 - 365 * 86400, by=ADMIN), {"conversations": 1})
        with self.assertRaises(Error):
            self.store.conversation_timeline(old)
        self.store.conversation_timeline(new)

    def test_tenants_are_separate_files_and_another_tenants_file_is_refused(self):
        self.call()
        other = conversations.ConversationStore(pathlib.Path(self._tmp.name), tenant="other-prod")
        self.assertEqual(other.stats(), {"conversations": 0, "linked": 0, "review": 0})
        self.store.path.rename(other.path)
        with self.assertRaises(Error):
            other.stats()
        for bad in ("Acme", "a/b", "", "x" * 64):
            with self.assertRaises(Error):
                conversations.ConversationStore(pathlib.Path(self._tmp.name), tenant=bad)

    def test_the_file_is_private_and_the_ledger_holds_identifiers_only(self):
        cid = self.linked()
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.store.path.parent.stat().st_mode & 0o777), "0o700")
        text = json.dumps(self.events)
        self.assertIn(cid, text)
        for secret in ("alex.tan", "customer.example", "6123"):
            self.assertNotIn(secret, text.lower())


if __name__ == "__main__":
    unittest.main()
