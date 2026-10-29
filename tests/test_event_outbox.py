"""Credential-free checks for the digest-only event outbox and the Tier 0 `created_since` read behind event sources (#99, #77)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
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


outbox_module = _load("event_outbox_under_test", "platform/core/event_outbox.py")
tickets = _load("tickets_for_outbox", "platform/core/tickets.py")
REF = "ab" * 16
OTHER = "cd" * 16


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.box = outbox_module.EventOutbox(pathlib.Path(self._tmp.name) / "events")

    def test_events_are_read_in_order_once_from_a_saved_offset(self):
        self.assertEqual(self.box.read(0), ([], 0))
        self.assertTrue(self.box.append("chat.message", REF, at=100))
        self.assertTrue(self.box.append("chat.message", OTHER, at=101))
        events, offset = self.box.read(0)
        self.assertEqual([(item["ref"], item["at"]) for item in events], [(REF, 100), (OTHER, 101)])
        self.assertEqual(self.box.read(offset), ([], offset))
        self.box.append("chat.message", REF, at=102)
        later, after = self.box.read(offset)
        self.assertEqual([item["at"] for item in later], [102])
        self.assertGreater(after, offset)

    def test_a_batch_limit_stops_early_and_the_offset_resumes_there(self):
        for index in range(5):
            self.box.append("chat.message", REF, at=index)
        first, offset = self.box.read(0, limit=2)
        rest, _ = self.box.read(offset, limit=10)
        self.assertEqual(([item["at"] for item in first], [item["at"] for item in rest]), ([0, 1], [2, 3, 4]))

    def test_only_a_known_kind_a_digest_and_a_time_are_accepted_so_no_identifier_or_text_can_enter(self):
        for kind, ref, at in (("ticket.created", REF, 1), ("chat.message", "C0123/1700000000.000200", 1), ("chat.message", REF[:-1], 1),
                              ("chat.message", REF.upper(), 1), ("chat.message", REF, -1), ("chat.message", REF, True)):
            with self.assertRaises(ValueError, msg=(kind, ref, at)):
                self.box.append(kind, ref, at=at)
        self.assertFalse(self.box.path.exists())

    def test_it_is_private_and_bounded_so_an_absent_reader_cannot_fill_the_volume(self):
        self.box.append("chat.message", REF, at=1)
        self.assertEqual(stat.S_IMODE(self.box.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.box.root.stat().st_mode), 0o700)
        line = len(self.box.path.read_bytes())
        self.box.path.write_bytes(b"x" * (outbox_module.MAX_BYTES - line))  # nearly full
        self.assertTrue(self.box.append("chat.message", REF, at=2))
        self.assertFalse(self.box.append("chat.message", REF, at=3))

    def test_corrupt_or_unterminated_event_refuses_the_whole_batch_instead_of_silently_dropping_work(self):
        for payload in (b"not json\n", json.dumps({"kind": "chat.message", "ref": "../x", "at": 2}).encode() + b"\n",
                        b'{"kind": "chat.message", "ref": "' + REF.encode()):
            with self.subTest(payload=payload[:24]), tempfile.TemporaryDirectory() as tmp:
                box = outbox_module.EventOutbox(pathlib.Path(tmp) / "events")
                box.append("chat.message", REF, at=1)
                with open(box.path, "ab") as handle:
                    handle.write(payload)
                with self.assertRaisesRegex(ValueError, "corrupt|incomplete|untrusted"):
                    box.read(0)
                with self.assertRaises(ValueError):
                    box.compact(box.path.stat().st_size)
                self.assertIn(payload, box.path.read_bytes())

    def test_invalid_or_stale_offset_never_replays_or_discards_events(self):
        self.box.append("chat.message", REF, at=1)
        events, offset = self.box.read(0)
        self.assertEqual(len(events), 1)
        for bad in (offset + 1, 1, -1, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.box.read(bad)
        self.assertEqual(self.box.read(offset), ([], offset))

    def test_deleted_initialized_outbox_is_not_mistaken_for_a_fresh_empty_workspace(self):
        self.box.append("chat.message", REF, at=1)
        self.assertTrue((self.box.root / ".events.initialized").exists())
        self.box.path.unlink()
        for operation in (lambda: self.box.read(0), lambda: self.box.append("chat.message", OTHER, at=2),
                          lambda: self.box.compact(0)):
            with self.assertRaisesRegex(ValueError, "missing|untrusted"):
                operation()
        self.assertFalse(self.box.path.exists())

    def test_linked_or_public_event_file_cannot_be_read_appended_or_compacted(self):
        for attack in ("symlink", "hardlink", "permissive", "fifo"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                box = outbox_module.EventOutbox(pathlib.Path(tmp) / "events")
                box.append("chat.message", REF, at=1)
                original = box.path.read_bytes()
                box.path.unlink()
                outside = box.root / "outside.jsonl"
                outside.write_bytes(original)
                outside.chmod(0o600)
                if attack == "symlink":
                    box.path.symlink_to(outside)
                elif attack == "hardlink":
                    os.link(outside, box.path)
                elif attack == "permissive":
                    box.path.write_bytes(original)
                    box.path.chmod(0o644)
                else:
                    os.mkfifo(box.path)
                for operation in (lambda: box.read(0), lambda: box.append("chat.message", OTHER, at=2),
                                  lambda: box.compact(0)):
                    with self.assertRaises((OSError, ValueError)):
                        operation()
                self.assertEqual(outside.read_bytes(), original)

    def test_untrusted_lock_and_directory_refuse_all_operations(self):
        for attack in ("lock-symlink", "lock-hardlink", "lock-public", "root-public", "root-symlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                box = outbox_module.EventOutbox(pathlib.Path(tmp) / "events")
                box.append("chat.message", REF, at=1)
                lock = box.root / ".events.lock"
                if attack == "lock-symlink":
                    lock.rename(box.root / "original.lock")
                    lock.symlink_to(box.root / "original.lock")
                elif attack == "lock-hardlink":
                    os.link(lock, box.root / "other.lock")
                elif attack == "lock-public":
                    lock.chmod(0o666)
                elif attack == "root-public":
                    box.root.chmod(0o755)
                else:
                    box.root.rename(box.root.with_name("original-events"))
                    box.root.symlink_to(box.root.with_name("original-events"), target_is_directory=True)
                for operation in (lambda: box.read(0), lambda: box.append("chat.message", OTHER, at=2),
                                  lambda: box.compact(0)):
                    with self.assertRaises((OSError, ValueError)):
                        operation()

    def test_it_empties_only_when_everything_was_read_and_the_file_is_large(self):
        self.box.append("chat.message", REF, at=1)
        _, offset = self.box.read(0)
        self.assertEqual(self.box.compact(offset), offset)  # small: left alone
        for index in range(outbox_module.COMPACT_BYTES // 60 + 10):
            self.box.append("chat.message", REF, at=index)
        size = self.box.path.stat().st_size
        self.assertGreaterEqual(size, outbox_module.COMPACT_BYTES)
        self.assertEqual(self.box.compact(size - 1), size - 1)  # something unread: left alone
        self.assertEqual(self.box.compact(size), 0)
        self.assertEqual(self.box.path.stat().st_size, 0)
        self.assertTrue(self.box.append("chat.message", OTHER, at=9))
        self.assertEqual([item["ref"] for item in self.box.read(0)[0]], [OTHER])


class CreatedSinceTests(unittest.TestCase):
    def test_it_lists_ids_and_times_only_oldest_first_and_includes_the_boundary_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = tickets.TicketStore(pathlib.Path(tmp), tenant="acme")
            old = store.create(project="support", title="Old one", requested_by="a@x.example", now=100)["ticketId"]
            same = store.create(project="support", title="Same second", requested_by="a@x.example", now=200)["ticketId"]
            new = store.create(project="support", title="New one, card 4111 1111 1111 1111", requested_by="b@x.example", now=300)["ticketId"]
            found = store.created_since(200)
            self.assertEqual([item["ticketId"] for item in found], [same, new])
            self.assertEqual(set(found[0]), {"ticketId", "createdAt"})
            self.assertNotIn(old, [item["ticketId"] for item in found])
            self.assertEqual(store.created_since(301), [])
            self.assertNotIn("4111", json.dumps(found))


if __name__ == "__main__":
    unittest.main()
