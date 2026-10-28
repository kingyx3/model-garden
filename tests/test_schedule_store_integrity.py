"""Adversarial credential-free tests for the private schedule/trigger store (#99).

A verified owner and budget do not help if another host process can replace
schedule state, lock files or the predictable temporary write destination.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("schedule_store_integrity_under_test", ROOT / "platform/core/schedules.py")
schedules = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = schedules
spec.loader.exec_module(schedules)


class ScheduleStoreIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "private"
        self.store = schedules.ScheduleStore(self.root, tenant="acme-dev")

    def create(self):
        return self.store.create(owner="owner@example.test", agent="receptionist",
                                 goal="Send synthetic pipeline summary",
                                 cron="0 9 * * 1", budget={"steps": 5},
                                 now=1_800_000_000)

    def test_private_round_trip_survives_restart(self):
        entry = self.create()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(self.store.path.stat().st_nlink, 1)
        restarted = schedules.ScheduleStore(self.root, tenant="acme-dev")
        self.assertEqual(restarted.get(entry["id"])["goal"], "Send synthetic pipeline summary")

    def test_root_symlink_is_rejected_without_touching_target(self):
        target = self.base / "target"
        target.mkdir(mode=0o700)
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(schedules.ScheduleError):
            self.store.list()
        self.assertEqual(list(target.iterdir()), [])

    def test_permissive_root_is_rejected_not_silently_chmodded(self):
        self.root.mkdir(mode=0o755)
        with self.assertRaises(schedules.ScheduleError):
            self.store.list()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)

    def test_state_symlink_and_hardlink_are_rejected(self):
        self.root.mkdir(mode=0o700)
        victim = self.base / "victim.json"
        victim.write_text('{"secret":"untouched"}')
        victim.chmod(0o600)
        self.store.path.symlink_to(victim)
        with self.assertRaises(schedules.ScheduleError):
            self.create()
        self.assertEqual(victim.read_text(), '{"secret":"untouched"}')
        self.store.path.unlink()
        os.link(victim, self.store.path)
        with self.assertRaises(schedules.ScheduleError):
            self.store.list()
        self.assertEqual(victim.read_text(), '{"secret":"untouched"}')

    def test_lock_symlink_and_hardlink_are_rejected(self):
        self.root.mkdir(mode=0o700)
        victim = self.base / "victim.lock"
        victim.write_text("do not touch")
        victim.chmod(0o600)
        lock = self.root / ".acme-dev.schedules.lock"
        lock.symlink_to(victim)
        with self.assertRaises(schedules.ScheduleError):
            self.create()
        self.assertEqual(victim.read_text(), "do not touch")
        lock.unlink()
        os.link(victim, lock)
        with self.assertRaises(schedules.ScheduleError):
            self.store.list()
        self.assertEqual(victim.read_text(), "do not touch")

    def test_group_world_accessible_state_and_lock_fail_closed(self):
        self.create()
        lock = self.root / ".acme-dev.schedules.lock"
        for path in (self.store.path, lock):
            path.chmod(0o644)
            with self.subTest(path=path), self.assertRaises(schedules.ScheduleError):
                self.store.list()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            path.chmod(0o600)

    def test_predictable_tmp_hardlink_cannot_truncate_unrelated_file(self):
        self.root.mkdir(mode=0o700)
        victim = self.base / "private-victim"
        victim.write_text("sentinel: keep intact")
        victim.chmod(0o600)
        os.link(victim, self.store.path.with_suffix(".tmp"))
        self.create()
        self.assertEqual(victim.read_text(), "sentinel: keep intact")
        self.assertEqual(self.store.list()[0]["goal"], "Send synthetic pipeline summary")

    def test_invalid_json_and_oversized_state_fail_closed(self):
        self.create()
        self.store.path.write_text("{invalid")
        with self.assertRaises(schedules.ScheduleError):
            self.store.list()
        self.store.path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        with self.assertRaises(schedules.ScheduleError):
            self.store.list()

    def test_missing_and_foreign_tenant_state_fail_closed(self):
        self.create()
        self.store.path.write_text('{"schemaVersion":1,"tenant":"foreign","schedules":{}}')
        with self.assertRaises(schedules.ScheduleError):
            self.store.list()
        self.store.path.unlink()
        self.assertEqual(self.store.list(), [])


if __name__ == "__main__":
    unittest.main()
