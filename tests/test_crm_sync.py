"""Credential-free checks for provider-neutral per-interaction CRM sync states."""
from __future__ import annotations

import importlib.util
import json
import pathlib
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


sync = _load("crm_sync_under_test", "platform/core/crm_sync.py")
govern = _load("govern_for_crm_sync", "scripts/govern-action.py")
Error = sync.SyncError
T0 = 1_800_000_000
CALL = "CA-8f3c2e"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "sync"
        self.events: list[tuple] = []

    def ledger(self, tenant: str = "acme-prod"):
        return sync.SyncLedger(self.root, tenant=tenant, audit=lambda *event: self.events.append(event))

    def enqueue(self, ledger=None, **overrides):
        values = dict(channel="voice", external_id=CALL, interaction_type="call", now=T0)
        values.update(overrides)
        return (ledger or self.ledger()).enqueue(**values)

    def test_the_same_call_or_message_always_has_the_same_key(self):
        one = sync.idempotency_key("acme-prod", "voice", CALL, "call")
        self.assertEqual(one, sync.idempotency_key("acme-prod", "voice", CALL, "call"))
        for other in (("globex-prod", "voice", CALL, "call"), ("acme-prod", "email", CALL, "email"),
                      ("acme-prod", "voice", "CA-other", "call"), ("acme-prod", "voice", CALL, "chat")):
            self.assertNotEqual(one, sync.idempotency_key(*other))

    def test_redelivery_restart_and_concurrent_enqueues_never_duplicate(self):
        first = self.enqueue()
        again = self.enqueue(now=T0 + 99)  # redelivered webhook
        restarted = self.enqueue(self.ledger())  # new process
        self.assertEqual((first["duplicate"], again["duplicate"], restarted["duplicate"]), (False, True, True))
        self.assertEqual(again["createdAt"], T0)
        self.assertEqual(len(self.ledger().list()), 1)
        results: list[bool] = []
        threads = [threading.Thread(target=lambda: results.append(self.enqueue(external_id="CA-race")["duplicate"])) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False] + [True] * 7)
        self.assertEqual(len(self.ledger().list()), 2)

    def test_invalid_identifiers_are_refused(self):
        for overrides in ({"channel": "fax"}, {"interaction_type": "sms"}, {"external_id": ""}, {"external_id": "a b"},
                          {"external_id": "x" * 300}, {"external_id": "../etc"}):
            with self.subTest(overrides=overrides), self.assertRaises(Error):
                self.enqueue(**overrides)

    def test_synced_needs_a_provider_acknowledgement(self):
        ledger = self.ledger()
        key = self.enqueue(ledger)["key"]
        for ref in ("", None, "has space", 12):
            with self.subTest(ref=ref), self.assertRaises(Error):
                ledger.record_success(key, provider_ref=ref, now=T0 + 1)
        self.assertEqual(ledger.get(key)["state"], "pending")
        done = ledger.record_success(key, provider_ref="hubspot:engagement/991", now=T0 + 2)
        self.assertEqual((done["state"], done["providerRef"]), ("synced", "hubspot:engagement/991"))
        message = sync.describe(done)
        self.assertIn("hubspot:engagement/991", message["message"])
        self.assertIsNone(message["repair"])

    def test_nothing_claims_success_before_acknowledgement(self):
        ledger = self.ledger()
        key = self.enqueue(ledger)["key"]
        for state in ("pending", "failed"):
            if state == "failed":
                ledger.record_failure(key, error_code="outage", now=T0 + 1)
            text = sync.describe(ledger.get(key))
            self.assertEqual(text["state"], state)
            self.assertIn("cannot say it was filed", text["message"])
            self.assertIsNone(text["providerRef"])

    def test_transient_failures_back_off_then_block_after_the_ceiling(self):
        ledger = self.ledger()
        key = self.enqueue(ledger)["key"]
        now, waits = T0, []
        for attempt in range(1, sync.MAX_ATTEMPTS):
            entry = ledger.record_failure(key, error_code="rate-limited", now=now)
            self.assertEqual(entry["state"], "failed")
            waits.append(entry["nextAttemptAt"] - now)
            self.assertEqual(ledger.due(now=entry["nextAttemptAt"] - 1), [])  # not before the backoff
            self.assertEqual([item["key"] for item in ledger.due(now=entry["nextAttemptAt"])], [key])
            now = entry["nextAttemptAt"]
        self.assertEqual(waits[:4], [30, 60, 120, 240])
        self.assertTrue(all(wait <= 3600 for wait in waits))
        blocked = ledger.record_failure(key, error_code="outage", now=now)
        self.assertEqual((blocked["state"], blocked["attempts"]), ("blocked", sync.MAX_ATTEMPTS))
        self.assertEqual(ledger.due(now=now + 10 ** 6), [])
        self.assertIn("ask an operator to retry", sync.describe(blocked)["repair"])

    def test_permanent_errors_block_at_once_with_a_specific_repair(self):
        ledger = self.ledger()
        rejected = ledger.record_failure(self.enqueue(ledger)["key"], error_code="rejected", now=T0)
        unauthorized = ledger.record_failure(self.enqueue(ledger, external_id="CA-2")["key"], error_code="unauthorized", now=T0)
        self.assertEqual((rejected["state"], unauthorized["state"]), ("blocked", "blocked"))
        self.assertIn("field mapping", sync.describe(rejected)["repair"])
        self.assertIn("Reconnect the CRM", sync.describe(unauthorized)["repair"])
        with self.assertRaises(Error):
            ledger.record_failure(rejected["key"], error_code="outage", now=T0 + 1)  # no attempts while blocked
        with self.assertRaises(Error):
            ledger.record_success(rejected["key"], provider_ref="x:1", now=T0 + 1)
        with self.assertRaises(Error):
            ledger.record_failure(rejected["key"], error_code="mystery", now=T0)

    def test_a_named_person_retries_a_blocked_interaction(self):
        ledger = self.ledger()
        key = self.enqueue(ledger)["key"]
        ledger.record_failure(key, error_code="unauthorized", now=T0)
        with self.assertRaises(Error):
            ledger.retry(key, by="", now=T0 + 5)
        retried = ledger.retry(key, by="jordan@client.example", now=T0 + 5)
        self.assertEqual((retried["state"], retried["attempts"]), ("pending", 0))
        self.assertEqual([item["key"] for item in ledger.due(now=T0 + 5)], [key])
        self.assertEqual(self.events[-1][:2], ("crm-sync.retry", "jordan@client.example"))
        ledger.record_success(key, provider_ref="hubspot:1", now=T0 + 6)
        with self.assertRaises(Error):
            ledger.retry(key, by="jordan@client.example", now=T0 + 7)  # a filed interaction is not retried

    def test_reconciliation_links_an_existing_record_without_writing_again(self):
        ledger = self.ledger()
        key = self.enqueue(ledger)["key"]
        ledger.record_failure(key, error_code="outage", now=T0)  # the write may have landed before the timeout
        entry = ledger.reconcile(key, provider_ref="hubspot:engagement/991", now=T0 + 40)
        self.assertEqual((entry["state"], entry["providerRef"]), ("reconciled", "hubspot:engagement/991"))
        self.assertEqual(ledger.due(now=T0 + 10 ** 6), [])
        with self.assertRaises(Error):
            ledger.reconcile(key, provider_ref="", now=T0 + 41)
        synced = self.enqueue(ledger, external_id="CA-3")["key"]
        ledger.record_success(synced, provider_ref="hubspot:2", now=T0)
        with self.assertRaises(Error):
            ledger.reconcile(synced, provider_ref="hubspot:3", now=T0 + 1)

    def test_a_repeated_success_is_confirmed_but_never_rewritten_to_another_reference(self):
        ledger = self.ledger()
        key = self.enqueue(ledger)["key"]
        ledger.record_success(key, provider_ref="hubspot:1", now=T0)
        again = ledger.record_success(key, provider_ref="hubspot:1", now=T0 + 9)
        self.assertEqual((again["attempts"], again["syncedAt"]), (1, T0))
        with self.assertRaises(Error):
            ledger.record_success(key, provider_ref="hubspot:OTHER", now=T0 + 10)

    def test_states_summary_and_listing(self):
        ledger = self.ledger()
        keys = [self.enqueue(ledger, external_id=f"CA-{n}")["key"] for n in range(4)]
        ledger.record_success(keys[0], provider_ref="p:1", now=T0)
        ledger.record_failure(keys[1], error_code="outage", now=T0)
        ledger.record_failure(keys[2], error_code="rejected", now=T0)
        self.assertEqual(sync.summary(ledger.list()), {"pending": 1, "synced": 1, "failed": 1, "blocked": 1, "reconciled": 0})
        self.assertEqual(len(ledger.list(states=("blocked", "failed"))), 2)

    def test_tenant_isolation_and_private_files(self):
        ledger = self.ledger()
        self.enqueue(ledger)
        other = self.ledger("globex-prod")
        self.assertEqual(other.list(), [])
        (self.root / "globex-prod.crm-sync.json").write_text((self.root / "acme-prod.crm-sync.json").read_text())
        with self.assertRaises(Error):
            other.list()
        with self.assertRaises(Error):
            sync.SyncLedger(self.root, tenant="Bad Tenant")
        self.assertEqual((self.root / "acme-prod.crm-sync.json").stat().st_mode & 0o777, 0o600)

    def test_transitions_are_ledgered_without_interaction_content(self):
        ledger = self.ledger()
        key = self.enqueue(ledger, external_id="CA-secret-looking-id")["key"]
        ledger.record_failure(key, error_code="rate-limited", now=T0)
        ledger.record_success(key, provider_ref="hubspot:1", now=T0 + 31)
        self.assertEqual([event[0] for event in self.events], ["crm-sync.enqueue", "crm-sync.failed", "crm-sync.synced"])
        self.assertEqual(self.events[1][2]["error"], "rate-limited")
        self.assertNotIn("secret-looking", json.dumps(self.events))
        audit = pathlib.Path(self._tmp.name) / "audit.jsonl"
        for action, by, details in self.events:
            govern.record_operator_action(audit, action, by, details)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])


CONVERSATION = "cv-0123456789abcdef0123"


class ConversationIdTests(Case):
    def test_the_entry_carries_the_conversation_and_can_be_listed_by_it(self):
        ledger = self.ledger()
        entry = self.enqueue(ledger, conversation_id=CONVERSATION)
        self.assertEqual(entry["conversationId"], CONVERSATION)
        self.enqueue(ledger, external_id="CA-other")
        self.assertEqual([e["key"] for e in ledger.list(conversation_id=CONVERSATION)], [entry["key"]])
        self.assertEqual(sync.describe(ledger.get(entry["key"]))["conversationId"], CONVERSATION)
        self.assertIn(CONVERSATION, json.dumps(self.events[0][2]))

    def test_a_redelivery_attaches_a_missing_id_but_never_replaces_one(self):
        ledger = self.ledger()
        first = self.enqueue(ledger)
        self.assertNotIn("conversationId", first)
        again = self.enqueue(ledger, conversation_id=CONVERSATION)
        self.assertEqual(again["conversationId"], CONVERSATION)
        other = self.enqueue(ledger, conversation_id="cv-ffffffffffffffffffff")
        self.assertEqual(other["conversationId"], CONVERSATION)
        self.assertEqual(ledger.get(first["key"])["conversationId"], CONVERSATION)

    def test_a_malformed_id_is_refused(self):
        for bad in ("cv-xyz", "conv-0123456789abcdef0123", "cv-0123456789abcdef0123; drop", 5):
            with self.assertRaises(Error):
                self.enqueue(conversation_id=bad)

    def test_the_id_is_not_part_of_the_idempotency_key(self):
        self.assertEqual(self.enqueue(conversation_id=CONVERSATION)["key"], sync.idempotency_key("acme-prod", "voice", CALL, "call"))


if __name__ == "__main__":
    unittest.main()
