"""Fail-closed, credential-free worker notice persistence regressions (#231/#109).

These tests exercise local owner-alert durability only; no external notification is sent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker_notice_integrity_under_test", ROOT / "platform/runtime/worker.py")
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
assert spec.loader is not None
spec.loader.exec_module(worker)


class NoticeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.queue = worker.NoticeQueue(self.base / "worker")

    def add(self, ref="one"):
        return self.queue.add("mail-needs-person", "owner@example.test", "Review this", reference=ref)

    def test_root_symlink_cannot_redirect_owner_alerts(self):
        target = self.base / "other"
        target.mkdir(mode=0o700)
        self.queue.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.add()
        self.assertEqual(list(target.iterdir()), [])

    def test_foreign_or_exposed_directory_refuses_writes(self):
        self.queue.root.mkdir(mode=0o700)
        self.queue.root.chmod(0o755)
        with self.assertRaises(ValueError):
            self.add()
        self.assertFalse(self.queue.path.exists())

    def test_symlinked_lock_cannot_clobber_unrelated_file(self):
        self.queue.root.mkdir(mode=0o700)
        victim = self.base / "victim"
        victim.write_text("KEEP")
        (self.queue.root / ".worker-notices.lock").symlink_to(victim)
        with self.assertRaises((ValueError, OSError)):
            self.add()
        self.assertEqual(victim.read_text(), "KEEP")

    def test_hardlinked_lock_is_refused(self):
        self.queue.root.mkdir(mode=0o700)
        victim = self.base / "victim"
        victim.write_text("KEEP")
        os.chmod(victim, 0o600)
        os.link(victim, self.queue.root / ".worker-notices.lock")
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(victim.read_text(), "KEEP")

    def test_symlinked_or_hardlinked_state_is_not_followed(self):
        for linked in ("symlink", "hardlink"):
            with self.subTest(linked=linked):
                self.queue.root.mkdir(mode=0o700, exist_ok=True)
                victim = self.base / "victim"
                victim.write_text('{"pending": {}}')
                victim.chmod(0o600)
                if linked == "symlink":
                    self.queue.path.symlink_to(victim)
                else:
                    os.link(victim, self.queue.path)
                with self.assertRaises((ValueError, OSError)):
                    self.add()
                self.assertEqual(victim.read_text(), '{"pending": {}}')
                self.queue.path.unlink()

    def test_exposed_or_malformed_state_is_not_silently_reset(self):
        self.add()
        self.queue.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.add("two")
        self.queue.path.chmod(0o600)
        self.queue.path.write_text('{"pending": []}')
        with self.assertRaises(ValueError):
            self.add("two")
        self.assertEqual(self.queue.path.read_text(), '{"pending": []}')

    def test_predictable_tmp_sentinel_is_never_overwritten(self):
        self.queue.root.mkdir(mode=0o700)
        victim = self.base / "victim"
        victim.write_text("KEEP")
        (self.queue.root / "worker-notices.tmp").symlink_to(victim)
        self.add()
        self.assertEqual(victim.read_text(), "KEEP")
        self.assertEqual(len(self.queue.pending()), 1)
        self.assertEqual(stat.S_IMODE(self.queue.path.stat().st_mode), 0o600)

    def test_concurrent_writers_keep_every_notice_after_restart(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(self.add, [f"thread-{i}" for i in range(24)]))
        restarted = worker.NoticeQueue(self.queue.root)
        self.assertEqual(len(restarted.pending()), 24)
        self.assertEqual(len({n["key"] for n in restarted.pending()}), 24)
        self.assertEqual(stat.S_IMODE(self.queue.root.stat().st_mode), 0o700)

    def test_invalid_ack_audit_cannot_remove_notice(self):
        key = self.add()
        with self.queue._locked() as state:
            state["pending"][key]["inboxPosted"] = True
        with self.assertRaises(RuntimeError):
            self.queue.ack_inbox(key, actor="operator", audit=lambda *_: (_ for _ in ()).throw(RuntimeError("ledger unavailable")))
        self.assertEqual(len(worker.NoticeQueue(self.queue.root).pending()), 1)


if __name__ == "__main__":
    unittest.main()
