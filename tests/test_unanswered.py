"""Credential-free checks for the unanswered-question store and its hooks (#237): grouping, masking, expiry, isolation, boundary, inbox, CLI."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_coworker_pause as paused  # noqa: E402
import test_digest_alerts as digest_tests  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
unanswered = paused._load("unanswered_under_test", "platform/core/unanswered.py")
NOW = 1_800_000_000
PRIVATE = "Alex Tan on +65 6123 4567 or alex.tan@acme.example"
PHONE = "+65 6123 4567"


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.events = []
        self.store = unanswered.UnansweredStore(self.root, tenant="acme", audit=lambda action, by, details: self.events.append((action, by, details)))

    def test_similar_questions_group_and_different_ones_do_not(self):
        first = self.store.record("What are your opening hours?", channel="slack", reason="no-match", now=NOW)
        again = self.store.record("opening hours, what are they", channel="email", reason="no-match", now=NOW + 60)
        self.assertEqual((again["id"], again["count"], again["channels"]), (first["id"], 2, {"slack": 1, "email": 1}))
        self.assertEqual((again["firstAt"], again["latestAt"]), (NOW, NOW + 60))
        other = self.store.record("Do you repair boilers on Sundays?", channel="slack", reason="no-match", now=NOW + 90)
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual([g["count"] for g in self.store.list()], [2, 1])  # most asked first
        self.assertIsNone(self.store.record("???  ", channel="slack", reason="x", now=NOW))
        self.assertIsNone(self.store.record("the and for", channel="slack", reason="x", now=NOW))  # nothing significant to group by

    def test_the_sample_masks_contact_details_links_and_ids_and_is_bounded(self):
        group = self.store.record(f"Please ring {PRIVATE} about https://acme.example/x?token=abc and ref ABCDEFGH12345678 {'warranty ' * 40}",
                                  channel="slack", reason="no-match", now=NOW)
        sample = group["sample"]
        for private in ("6123", "alex.tan", "acme.example", "ABCDEFGH12345678", "token"):
            self.assertNotIn(private, sample)
        for mask in ("[number]", "[email]", "[link]", "[id]"):
            self.assertIn(mask, sample)
        self.assertLessEqual(len(sample), unanswered.MAX_SAMPLE)
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")

    def test_status_changes_are_ledgered_by_name_without_the_sample_and_reopen_works(self):
        group = self.store.record("Do you offer student discounts?", channel="web", reason="no-match", now=NOW)
        closed = self.store.set_status(group["id"], "dismissed", by="jordan@acme.example", now=NOW + 5)
        self.assertEqual((closed["status"], closed["decidedBy"]), ("dismissed", "jordan@acme.example"))
        self.assertEqual(self.store.list(status="open"), [])
        self.store.set_status(group["id"], "open", by="jordan@acme.example", now=NOW + 6)
        self.assertEqual(len(self.store.list(status="open")), 1)
        self.assertEqual([event[0] for event in self.events], ["unanswered.status", "unanswered.status"])
        self.assertNotIn("student", json.dumps(self.events))
        for bad in (("q-nothex", "open", "a"), (group["id"], "gone", "a"), (group["id"], "open", " "), ("q-000000000000", "open", "a")):
            with self.assertRaises(unanswered.UnansweredError):
                self.store.set_status(bad[0], bad[1], by=bad[2], now=NOW)

    def test_old_groups_expire_and_the_ledger_records_only_counts(self):
        self.store.record("Do you offer student discounts?", channel="web", reason="x", now=NOW - 100 * 86_400)
        self.store.record("Where can customers park?", channel="web", reason="x", now=NOW - 5 * 86_400)
        self.assertEqual(self.store.purge_expired(NOW), 1)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.events[-1][2], {"groups": 1, "retentionDays": 90})
        self.assertEqual(self.store.purge_expired(NOW), 0)

    def test_tenants_are_isolated_and_the_store_is_bounded(self):
        other = unanswered.UnansweredStore(self.root, tenant="beta")
        self.store.record("Do you offer student discounts?", channel="web", reason="x", now=NOW)
        self.assertEqual(other.list(), [])
        with self.assertRaises(unanswered.UnansweredError):
            unanswered.UnansweredStore(self.root, tenant="../etc")
        original = unanswered.MAX_GROUPS
        unanswered.MAX_GROUPS = 3
        try:
            for index, word in enumerate(("alpha", "bravo", "charlie", "delta", "echo")):
                group = self.store.record(f"question about {word} service", channel="web", reason="x", now=NOW + index)
                self.store.set_status(group["id"], "dismissed", by="a", now=NOW)
        finally:
            unanswered.MAX_GROUPS = original
        self.assertLessEqual(len(self.store.list()), 4)  # the oldest closed groups went first


class BoundaryTests(unittest.TestCase):
    setUp = paused.BoundaryTests.setUp
    events = paused.BoundaryTests.events

    def store(self):
        return unanswered.UnansweredStore(self.root / "unanswered", tenant="default")

    def test_a_search_with_no_approved_answer_is_noted_and_one_with_an_answer_is_not(self):
        self.assertEqual(self.runtime.kb_search(query="opening hours")["status"], "executed")
        self.assertEqual(self.store().list(), [])
        result = self.runtime.kb_search(query=f"Can you ship a canoe, ring {PHONE}?")
        self.assertEqual(result["result"]["documents"], [])
        groups = self.store().list()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 1)
        self.assertNotIn("6123", groups[0]["sample"])
        self.assertNotIn("canoe", (self.root / "audit.jsonl").read_text())  # the ledger never holds the question text
        self.runtime.kb_search(query=f"can you ship a canoe ring {PHONE}")  # the same significant words, other punctuation
        self.assertEqual(self.store().list()[0]["count"], 2)

    def test_a_note_that_cannot_be_written_never_stops_the_answer(self):
        (self.root / "unanswered").write_text("a file where the directory should be")
        self.assertEqual(self.runtime.kb_search(query="Can you ship a canoe?")["status"], "executed")

    def test_noting_continues_while_paused_because_search_is_a_read(self):
        paused.pause.PauseStore(self.root).pause("alice@acme.example", "incident", NOW)
        self.runtime.kb_search(query="Can you ship a canoe?")
        self.assertEqual(len(self.store().list()), 1)


class InboxAndCliTests(digest_tests.DigestCase):
    def test_the_inbox_shows_a_count_not_the_text_and_the_digest_and_retention_use_it(self):
        inbox = paused.inbox
        directory = self.data / "unanswered"
        store = unanswered.UnansweredStore(directory, tenant="acme")
        store.record(f"Can you ship a canoe to {PRIVATE}?", channel="web", reason="no-match", now=NOW - 7200)
        store.record("Can you ship a canoe?", channel="web", reason="no-match", now=NOW - 60)
        snapshot = inbox.collect(approvals=self.data / "approvals", handoffs=self.data / "handoffs", unanswered=directory, now=NOW)
        item = next(entry for entry in snapshot["items"] if entry["kind"] == "unanswered")
        self.assertEqual((item["count"], item["questions"], snapshot["counts"]["unanswered"]), (2, 2, 2))  # grouping is conservative: different words, different groups
        for rendered in (inbox.format_text(snapshot), inbox.format_chat(inbox.for_person(snapshot, "a@b.example", everything=True))):
            self.assertNotIn("canoe", rendered)
            self.assertIn("nanswered", rendered)
        self.assertEqual(inbox.for_person(snapshot, "a@b.example", everything=False)["counts"]["unanswered"], 0)  # not for ordinary people
        runtime = self.make(self.CONFIG)
        runtime.run_digest(DigestTests_MONDAY)
        self.assertIn("2 unanswered question group(s)", self.notices("digest")[0]["text"])
        self.assertEqual(runtime.purge_unanswered(NOW + 200 * 86_400), 2)  # long after: both groups expired
        self.assertEqual(runtime.run_retention(NOW + 201 * 86_400)["unanswered"], 0)

    def test_the_operator_script_lists_dismisses_and_ledgers_under_the_named_person(self):
        directory = self.root / "u"
        group = unanswered.UnansweredStore(directory, tenant="acme").record("Can you ship a canoe?", channel="web", reason="x", now=NOW)
        audit = self.root / "audit.jsonl"

        def run(*args):
            return subprocess.run([sys.executable, str(ROOT / "scripts/runtime-unanswered.py"), str(directory), "--tenant", "acme", "--audit-path", str(audit), *args],
                                  capture_output=True, text=True)

        listed = run("list")
        self.assertEqual(listed.returncode, 0)
        self.assertIn(group["id"], listed.stdout)
        self.assertEqual(run("dismiss", group["id"], "--by", "jordan@acme.example").returncode, 0)
        self.assertEqual(run("list", "--status", "open").stdout, "")
        actions = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual([(e["action"], e["performedBy"]) for e in actions], [("unanswered.status", "jordan@acme.example")])
        self.assertNotIn("canoe", audit.read_text())
        self.assertEqual(run("dismiss", "q-000000000000", "--by", "jordan@acme.example").returncode, 2)


DigestTests_MONDAY = digest_tests.DigestTests.MONDAY


if __name__ == "__main__":
    unittest.main()
