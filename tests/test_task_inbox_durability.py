"""Fail-closed cross-process Agent task inbox regressions (#229).

Synthetic local filesystem tests only: they do not execute a model or prove live channel delivery.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("task_inbox_security", ROOT / "platform/core/task_inbox.py")
inbox_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = inbox_module
spec.loader.exec_module(inbox_module)


class DurableTaskInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.path = self.root / "tasks" / "inbox.json"
        self.store = inbox_module.TaskInbox(self.path)

    def assign(self, task="t1"):
        return self.store.assign("receptionist", task, assigner="person:alice@example.test",
                                 goal="Review a synthetic task", root=task, parent=None, now=100)

    def victim(self):
        path = self.root / "victim"
        path.write_text("do-not-change", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_assignment_report_and_restart_remain_durable(self):
        self.assign()
        self.assertEqual(self.store.pending("receptionist")[0]["state"], "assigned")
        self.store.take_up("receptionist", "t1", now=101)
        with self.assertRaises(inbox_module.InboxError):
            self.store.take_up("receptionist", "t1", now=102)
        self.store.report("receptionist", "t1", "done", "Finished", now=103)
        self.assertEqual(inbox_module.TaskInbox(self.path).get("t1")["report"], "Finished")
        self.assertEqual(self.store.pending("receptionist"), [])

    def test_symlinked_lock_is_refused_without_touching_target(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.victim()
        self.path.with_suffix(".lock").symlink_to(target)
        with self.assertRaises((OSError, inbox_module.InboxError)):
            self.assign()
        self.assertEqual(target.read_text(), "do-not-change")
        self.assertFalse(self.path.exists())

    def test_hardlinked_lock_is_refused_without_touching_target(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.victim()
        os.link(target, self.path.with_suffix(".lock"))
        with self.assertRaises((OSError, inbox_module.InboxError)):
            self.assign()
        self.assertEqual(target.read_text(), "do-not-change")

    def test_symlinked_or_hardlinked_task_state_is_refused(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.victim()
        self.path.symlink_to(target)
        with self.assertRaises((OSError, inbox_module.InboxError)):
            self.assign()
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises((OSError, inbox_module.InboxError)):
            self.assign()
        self.assertEqual(target.read_text(), "do-not-change")

    def test_predictable_old_temporary_hardlink_is_never_truncated(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.victim()
        os.link(target, self.path.with_suffix(".tmp"))
        self.assign()
        self.assertEqual(target.read_text(), "do-not-change")
        self.assertEqual(self.store.get("t1")["state"], "assigned")

    def test_exposed_permissions_and_malformed_tasks_fail_closed(self):
        self.path.parent.mkdir(mode=0o700)
        self.path.write_text(json.dumps({"schemaVersion": 1, "tasks": {}}))
        self.path.chmod(0o644)
        with self.assertRaises((OSError, inbox_module.InboxError)):
            self.assign()
        self.path.write_text(json.dumps({"schemaVersion": 1, "tasks": []}))
        self.path.chmod(0o600)
        with self.assertRaises((OSError, inbox_module.InboxError)):
            self.assign()

    def test_oversized_state_is_refused_without_reinitializing(self):
        self.path.parent.mkdir(mode=0o700)
        self.path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        self.path.chmod(0o600)
        with self.assertRaises((OSError, inbox_module.InboxError)):
            self.assign()
        self.assertGreater(self.path.stat().st_size, 8 * 1024 * 1024)

    def test_concurrent_assignments_do_not_lose_or_duplicate_tasks(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.assign(f"t{i}"), range(24)))
        rows = inbox_module.TaskInbox(self.path).pending("receptionist")
        self.assertEqual({row["taskId"] for row in rows}, {f"t{i}" for i in range(24)})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
