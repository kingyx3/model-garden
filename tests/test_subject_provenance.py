"""Synthetic, test-first provenance and disclosure boundaries for Tier 0 subject exports (#241)."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_subject_access as existing  # noqa: E402


class ProvenanceTests(existing.Case):
    def test_index_distinguishes_included_records_from_unverified_other_stores(self):
        result = self.export()
        bundle = json.loads(pathlib.Path(result["path"]).read_text())
        index = bundle["provenanceIndex"]
        stores = {entry["store"]: entry for entry in index["stores"]}
        self.assertEqual(stores["built-in-contacts"]["records"], 1)
        self.assertEqual(stores["built-in-interactions"]["records"], 1)
        self.assertEqual(stores["conversation-timeline"]["records"], 1)
        for name in ("voice-transcript-content", "agent-memory", "crm-sync-state"):
            self.assertEqual(stores[name]["status"], "not-indexed")
            self.assertIsNone(stores[name]["records"])
        self.assertEqual(index["linkage"], "explicit-contact-or-confirmed-conversation-only")
        self.assertEqual(len(index["references"]), 1)
        self.assertEqual(index["references"][0]["kind"], "message-reference")
        self.assertNotIn("message:m-1", json.dumps(index))
        self.assertNotIn("Alex", json.dumps(index))
        self.assertNotIn("Priya", json.dumps(index))

    def test_transcript_references_are_digests_not_contents_or_verified_transcripts(self):
        self.contacts.log_interaction({
            "objectId": self.alex, "interactionType": "call", "channel": "voice",
            "direction": "inbound", "timestamp": "2026-10-21T10:00:00+08:00",
            "summary": "Synthetic caller summary", "transcriptRef": "transcript:call-001",
        })
        linked = self.conversations.open("voice", "T1/C2", agent="receptionist", now=existing.NOW)
        self.conversations.add_interaction(linked["id"], kind="call", external_id="c2",
                                           at=existing.NOW, ref="transcript:call-002")
        self.conversations.link(linked["id"], self.alex, by="jordan@acme.example", now=existing.NOW)
        orphan = self.conversations.open("voice", "T1/foreign", agent="receptionist", now=existing.NOW)
        self.conversations.add_interaction(orphan["id"], kind="call", external_id="foreign",
                                           at=existing.NOW, ref="transcript:foreign-secret")
        bundle = json.loads(pathlib.Path(self.export()["path"]).read_text())
        index = bundle["provenanceIndex"]
        refs = index["references"]
        digests = {row["referenceDigest"] for row in refs}
        self.assertIn(hashlib.sha256(b"transcript:call-001").hexdigest(), digests)
        self.assertIn(hashlib.sha256(b"transcript:call-002").hexdigest(), digests)
        self.assertNotIn(hashlib.sha256(b"transcript:foreign-secret").hexdigest(), digests)
        self.assertEqual(len(refs), 3)
        self.assertTrue(all(row["contentIncluded"] is False for row in refs))
        self.assertNotIn("transcript:call-001", json.dumps(index))
        self.assertNotIn("transcript:call-002", json.dumps(index))
        self.assertNotIn("transcript:foreign-secret", json.dumps(bundle))

    def test_new_linked_reference_requires_fresh_approval_not_silent_retry(self):
        first = self.export()
        self.conversations.add_interaction(
            self.conversations.open("chat", "T1/C1", agent="receptionist", now=existing.NOW)["id"],
            kind="message", external_id="m2", at=existing.NOW, ref="message:m-2",
        )
        with self.assertRaisesRegex(existing.subject.SubjectAccessError, "changed|new approval"):
            self.export()
        self.assertEqual(len([e for e in self.events if e[0] == "subject-access.exported"]), 1)
        self.assertFalse(first["repeat"])
