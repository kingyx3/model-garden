"""Adversarial, credential-free tests for shared gateway/worker heartbeat integrity (#231).

A forged healthy heartbeat must not mask a stopped runtime. The writer must not
truncate an unrelated file through a predictable temporary hardlink.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("heartbeat_storage_under_test", ROOT / "platform/core/heartbeat.py")
heartbeat = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(heartbeat)
NOW = 1_800_000_000


class HeartbeatStorageSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "worker" / "heartbeat.json"
        self.path.parent.mkdir()

    def test_normal_atomic_beat_is_private_and_healthy(self) -> None:
        document = heartbeat.Beat(self.path).write(NOW, {"scheduler": False})
        self.assertEqual(json.loads(self.path.read_text()), document)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(heartbeat.check(self.path, now=NOW)["status"], "healthy")

    def test_predictable_temp_hardlink_cannot_truncate_another_file(self) -> None:
        victim = self.root / "do-not-touch"
        victim.write_text("original private contents", encoding="utf-8")
        os.link(victim, self.path.with_suffix(".tmp"))
        heartbeat.Beat(self.path).write(NOW, {"scheduler": False})
        self.assertEqual(victim.read_text(encoding="utf-8"), "original private contents")
        self.assertEqual(heartbeat.check(self.path, now=NOW)["status"], "healthy")

    def test_symlink_hardlink_and_fifo_cannot_claim_healthy(self) -> None:
        target = self.root / "forged"
        target.write_text(json.dumps({"at": NOW, "failed": [], "persistentFailures": []}))
        self.path.symlink_to(target)
        self.assertEqual(heartbeat.check(self.path, now=NOW)["status"], "unavailable")
        self.path.unlink()
        os.link(target, self.path)
        self.assertEqual(heartbeat.check(self.path, now=NOW)["status"], "unavailable")
        self.path.unlink()
        os.mkfifo(self.path)
        # Must return without blocking on a FIFO whose writer never opens.
        self.assertEqual(heartbeat.check(self.path, now=NOW)["status"], "unavailable")

    def test_group_writable_heartbeat_cannot_claim_healthy(self) -> None:
        self.path.write_text(json.dumps({"at": NOW, "failed": [], "persistentFailures": []}))
        self.path.chmod(0o666)
        self.assertEqual(heartbeat.check(self.path, now=NOW)["status"], "unavailable")

    def test_swapped_inode_during_read_fails_closed(self) -> None:
        heartbeat.Beat(self.path).write(NOW, {"scheduler": False})
        original_read = os.read
        replaced = False

        def swap(fd: int, size: int) -> bytes:
            nonlocal replaced
            if not replaced:
                replaced = True
                self.path.unlink()
                self.path.write_text(json.dumps({"at": NOW, "failed": [], "persistentFailures": []}))
            return original_read(fd, size)

        with mock.patch.object(heartbeat.os, "read", side_effect=swap):
            result = heartbeat.check(self.path, now=NOW)
        self.assertTrue(replaced)
        self.assertEqual(result["status"], "unavailable")

    def test_symlinked_parent_is_never_used_for_read_or_write(self) -> None:
        self.path.parent.rmdir()
        external = self.root / "external"
        external.mkdir()
        (external / "heartbeat.json").write_text(json.dumps({"at": NOW, "failed": [], "persistentFailures": []}))
        self.path.parent.symlink_to(external, target_is_directory=True)
        self.assertEqual(heartbeat.check(self.path, now=NOW)["status"], "unavailable")
        with self.assertRaises(OSError):
            heartbeat.Beat(self.path).write(NOW, {"scheduler": False})


if __name__ == "__main__":
    unittest.main()
