"""Credential-free checks for the digest-only event outbox and the Tier 0 `created_since` read behind event sources (#99, #77)."""
from __future__ import annotations

import importlib.util
import json
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

    def test_a_half_written_or_corrupt_line_is_never_read_as_an_event(self):
        self.box.append("chat.message", REF, at=1)
        with open(self.box.path, "ab") as handle:
            handle.write(b"not json\n")
            handle.write(json.dumps({"kind": "chat.message", "ref": "../x", "at": 2}).encode() + b"\n")
            handle.write(b'{"kind": "chat.message", "ref": "' + REF.encode())  # still being written
        events, offset = self.box.read(0)
        self.assertEqual([item["at"] for item in events], [1])
        self.assertLess(offset, self.box.path.stat().st_size)  # stops before the unfinished line

    def test_a_replaced_or_shorter_file_restarts_the_reader_at_the_beginning(self):
        self.box.append("chat.message", REF, at=1)
        events, _ = self.box.read(10_000)
        self.assertEqual(len(events), 1)

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
