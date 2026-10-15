"""Credential-free replay and provider-swap checks for filing interactions through the sync ledger."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


filing = _load("crm_filing_under_test", "platform/core/crm_filing.py")
sync = filing._sync()
contacts = _load("contacts_for_filing", "platform/core/contacts.py")
hubspot = _load("hubspot_for_filing", "platform/connectors/hubspot.py")
govern = _load("govern_for_filing", "scripts/govern-action.py")
T0 = 1_800_000_000
SUMMARY = "Asked about opening hours; Hargreaves & Co want a Friday booking."
OPERATOR = "jordan@client.example"


def interaction(**overrides):
    value = {"interactionType": "call", "channel": "voice", "direction": "inbound", "timestamp": "2026-10-15T10:00:00+08:00",
             "summary": SUMMARY, "outcome": "booked", "transcriptRef": "transcript:abc123"}
    value.update(overrides)
    return value


class FakeHubSpot:
    """A HubSpot that stores notes, can fail on demand and can lose its answer after writing."""

    def __init__(self) -> None:
        self.notes: dict[str, dict] = {}
        self.fail_with: list[int] = []
        self.lose_answer_once = False
        self.writes = 0

    def __call__(self, method, url, headers, payload):
        if self.fail_with:
            return self.fail_with.pop(0), {"message": "boom"}
        path = urllib.parse.urlparse(url).path
        if path.endswith("/notes/search"):
            token = payload["filterGroups"][0]["filters"][0]["value"]
            return 200, {"results": [{"id": note_id} for note_id, note in self.notes.items() if token in note["properties"]["hs_note_body"]]}
        if path.endswith("/notes"):
            self.writes += 1
            note_id = str(900 + len(self.notes))
            self.notes[note_id] = payload
            if self.lose_answer_once:
                self.lose_answer_once = False
                raise TimeoutError("the response never arrived")
            return 201, {"id": note_id}
        return 404, {"message": "not found"}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.events: list[tuple] = []
        self.ledger = sync.SyncLedger(self.root / "sync", tenant="acme-prod", audit=lambda *event: self.events.append(event))
        self.fake = FakeHubSpot()
        self.hubspot = hubspot.HubSpotClient("tok", allowed_fields={"contact": ["email"]}, transport=self.fake)
        self.store = contacts.ContactStore(self.root / "contacts")

    def hs(self, **overrides):
        return {**interaction(), "objectId": "42", **overrides}

    def tier0(self, **overrides):
        contact = self.store.create_contact({"name": "Alex Tan", "email": "alex@customer.example"}, now=T0)
        return {**interaction(), "objectId": contact["objectId"], **overrides}

    def file(self, executor, arguments, *, external_id="CA-1", now=T0, interaction_type="call"):
        return filing.file_interaction(self.ledger, executor, arguments, channel="voice", external_id=external_id,
                                       interaction_type=interaction_type, now=now)

    # -- success, redelivery, replay ----------------------------------------------------

    def test_an_interaction_is_filed_and_tracked_with_the_providers_reference(self):
        outcome = self.file(self.hubspot.log_interaction, self.hs())
        self.assertEqual((outcome["state"], outcome["attempted"], outcome["duplicate"]), ("synced", True, False))
        self.assertTrue(outcome["providerRef"].startswith("hubspot:note/"))
        self.assertIsNone(outcome["repair"])
        self.assertEqual(len(self.fake.notes), 1)

    def test_redelivery_replay_and_restart_never_file_twice(self):
        calls = []
        executor = lambda args: calls.append(args) or self.hubspot.log_interaction(args)  # noqa: E731
        first = self.file(executor, self.hs())
        again = self.file(executor, self.hs(), now=T0 + 5)  # a redelivered webhook
        restarted = filing.file_interaction(
            sync.SyncLedger(self.root / "sync", tenant="acme-prod"), executor, self.hs(), channel="voice", external_id="CA-1",
            interaction_type="call", now=T0 + 9)  # a new process, same files
        self.assertEqual((first["state"], again["duplicate"], restarted["duplicate"]), ("synced", True, True))
        self.assertEqual((len(calls), len(self.fake.notes)), (1, 1))
        self.assertEqual(again["providerRef"], first["providerRef"])

    def test_a_different_call_is_a_different_entry(self):
        self.file(self.hubspot.log_interaction, self.hs())
        self.file(self.hubspot.log_interaction, self.hs(summary="A different call", timestamp="2026-10-15T11:00:00+08:00"), external_id="CA-2")
        self.assertEqual(len(self.fake.notes), 2)
        self.assertEqual(len(self.ledger.list()), 2)

    # -- failures: sorted, backed off, never claimed as success -------------------------

    def test_a_rate_limit_backs_off_and_is_retried_without_a_duplicate(self):
        self.fake.fail_with = [429]
        first = self.file(self.hubspot.log_interaction, self.hs())
        self.assertEqual((first["state"], first["attempts"]), ("failed", 1))
        self.assertIn("retried automatically", first["repair"])
        self.assertIn("cannot say it was filed", first["message"])
        early = self.file(self.hubspot.log_interaction, self.hs(), now=T0 + 10)  # before the 30 s backoff
        self.assertEqual((early["attempted"], early["state"]), (False, "failed"))
        later = self.file(self.hubspot.log_interaction, self.hs(), now=T0 + 31)
        self.assertEqual((later["state"], later["attempts"], len(self.fake.notes)), ("synced", 2, 1))

    def test_a_server_error_is_an_outage_that_blocks_after_the_ceiling(self):
        self.fake.fail_with = [500] * 20
        now = T0
        for _ in range(sync.MAX_ATTEMPTS):
            outcome = self.file(self.hubspot.log_interaction, self.hs(), now=now)
            now += 4000
        self.assertEqual((outcome["state"], outcome["attempts"]), ("blocked", sync.MAX_ATTEMPTS))
        self.assertIn("ask an operator to retry", outcome["repair"])
        self.assertEqual(self.file(self.hubspot.log_interaction, self.hs(), now=now + 10 ** 6)["attempted"], False)  # blocked entries are not retried

    def test_an_authorisation_failure_blocks_until_a_named_person_retries(self):
        self.fake.fail_with = [401]
        blocked = self.file(self.hubspot.log_interaction, self.hs())
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn("Reconnect the CRM", blocked["repair"])
        self.assertFalse(self.file(self.hubspot.log_interaction, self.hs(), now=T0 + 99)["attempted"])
        self.ledger.retry(blocked["key"], by=OPERATOR, now=T0 + 100)
        done = self.file(self.hubspot.log_interaction, self.hs(), now=T0 + 100)
        self.assertEqual((done["state"], len(self.fake.notes)), ("synced", 1))

    def test_failures_are_sorted_into_the_sync_vocabulary(self):
        cases = [
            (hubspot.HubSpotError("x", "rate-limited", 429), "rate-limited"), (hubspot.HubSpotError("x", "unauthorized", 401), "unauthorized"),
            (PermissionError("denied by policy"), "unauthorized"), (ValueError("bad arguments"), "rejected"),
            (contacts.ContactError("unknown contact"), "rejected"), (TimeoutError("slow"), "outage"), (OSError("down"), "outage"),
            (RuntimeError("???"), "outage"),
        ]
        for error, code in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(filing.classify(error), code)
                self.assertIn(code, sync.ERROR_CODES)

    def test_a_result_without_the_providers_reference_is_not_a_success(self):
        for result in ({}, {"externalObjectRef": ""}, {"interactionId": "i-1"}, {"externalObjectRef": "has space"}, None):
            ledger = sync.SyncLedger(self.root / f"s-{hash(str(result)) % 1000}", tenant="acme-prod")
            outcome = filing.file_interaction(ledger, lambda args, result=result: result, self.hs(), channel="voice", external_id="CA-9",
                                              interaction_type="call", now=T0)
            self.assertEqual(outcome["state"], "blocked", result)  # a malformed acknowledgement is a rejection, never "synced"
            self.assertIsNone(outcome["providerRef"])

    def test_a_lost_acknowledgement_is_reconciled_not_written_twice(self):
        self.fake.lose_answer_once = True
        first = self.file(self.hubspot.log_interaction, self.hs())
        self.assertEqual((first["state"], len(self.fake.notes)), ("failed", 1))  # it landed, but we never heard
        second = self.file(self.hubspot.log_interaction, self.hs(), now=T0 + 40)
        self.assertEqual((second["state"], second["duplicate"], len(self.fake.notes), self.fake.writes), ("reconciled", True, 1, 1))
        self.assertTrue(second["providerRef"].startswith("hubspot:note/"))

    # -- provider swap ------------------------------------------------------------------

    def test_the_same_interaction_files_through_either_crm_with_the_same_states(self):
        runs = {}
        for name, executor, arguments in (("hubspot", self.hubspot.log_interaction, self.hs()),
                                          ("built-in", lambda args: self.store.log_interaction(args, now=T0), self.tier0())):
            ledger = sync.SyncLedger(self.root / f"swap-{name}", tenant="acme-prod")
            first = filing.file_interaction(ledger, executor, arguments, channel="voice", external_id="CA-7", interaction_type="call", now=T0)
            replay = filing.file_interaction(ledger, executor, arguments, channel="voice", external_id="CA-7", interaction_type="call", now=T0 + 3)
            runs[name] = (first["state"], first["attempted"], replay["duplicate"], replay["providerRef"] == first["providerRef"])
            self.assertTrue(first["providerRef"].startswith(f"{'hubspot' if name == 'hubspot' else 'built-in-crm'}:"))
        self.assertEqual(runs["hubspot"], runs["built-in"])
        self.assertEqual(self.store.review_queue()["unmatchedInteractions"], [])  # filed against the contact, not left for review

    def test_the_built_in_log_reconciles_when_the_record_is_already_there(self):
        arguments = self.tier0()
        self.store.log_interaction(arguments, now=T0)  # filed before the ledger heard (a crash between the two)
        outcome = self.file(lambda args: self.store.log_interaction(args, now=T0 + 1), arguments)
        self.assertEqual((outcome["state"], outcome["duplicate"]), ("reconciled", True))

    def test_an_unknown_contact_is_a_rejection_that_waits_for_a_person(self):
        outcome = self.file(lambda args: self.store.log_interaction(args, now=T0), self.hs(objectId="c-unknown"))
        self.assertEqual(outcome["state"], "blocked")
        self.assertIn("field mapping", outcome["repair"])

    # -- retry runner and content ---------------------------------------------------------

    def test_retry_due_files_what_is_due_from_the_payload_source_and_skips_what_it_cannot_find(self):
        payloads = {"CA-1": self.hs(), "CA-2": self.hs(summary="Second call", timestamp="2026-10-15T12:00:00+08:00")}
        self.fake.fail_with = [503, 503, 500]
        for external_id, arguments in (("CA-1", payloads["CA-1"]), ("CA-2", payloads["CA-2"]),
                                       ("CA-3", self.hs(summary="Third call", timestamp="2026-10-15T13:00:00+08:00"))):
            self.assertEqual(self.file(self.hubspot.log_interaction, arguments, external_id=external_id)["state"], "failed")
        self.assertEqual(self.fake.notes, {})
        before_backoff = filing.retry_due(self.ledger, self.hubspot.log_interaction, lambda entry: payloads.get(entry["externalId"]), now=T0 + 5)
        self.assertEqual(before_backoff, {"attempted": 0, "synced": 0, "failed": 0, "blocked": 0, "skipped": 0})
        counts = filing.retry_due(self.ledger, self.hubspot.log_interaction, lambda entry: payloads.get(entry["externalId"]), now=T0 + 60)
        self.assertEqual((counts["attempted"], counts["synced"], counts["skipped"]), (2, 2, 1))  # CA-3's payload is unavailable: skipped, not invented
        self.assertEqual(len(self.fake.notes), 2)
        self.assertEqual(self.ledger.get(self.ledger.list(states=("failed",))[0]["key"])["externalId"], "CA-3")

    def test_the_sync_ledger_and_its_events_hold_no_interaction_content(self):
        self.fake.fail_with = [429]
        self.file(self.hubspot.log_interaction, self.hs())
        self.file(self.hubspot.log_interaction, self.hs(), now=T0 + 31)
        files = "".join(path.read_text() for path in (self.root / "sync").iterdir() if path.is_file())
        for secret in ("Hargreaves", "opening hours", "transcript:abc123", "booked"):
            self.assertNotIn(secret, files)
            self.assertNotIn(secret, json.dumps(self.events))
        audit = self.root / "audit.jsonl"
        for action, by, details in self.events:
            govern.record_operator_action(audit, action, by, details)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])
        self.assertEqual([event[0] for event in self.events], ["crm-sync.enqueue", "crm-sync.failed", "crm-sync.synced"])


if __name__ == "__main__":
    unittest.main()
