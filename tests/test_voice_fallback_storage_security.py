"""Fail-closed fallback-message storage regressions for P0 voice privacy (#107).

Pure Python: extract the transport-independent writer from the LiveKit bridge;
no provider credentials or LiveKit runtime are required.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fallback_private_transcript", ROOT / "platform/channels/call_transcript.py")
T = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = T
assert spec.loader is not None
spec.loader.exec_module(T)

source = ROOT / "platform/channels/livekit_receptionist.py"
tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {"_retention_seconds", "_retained_rows", "_append_message"}]
assert len(functions) == 3
namespace = {
    "os": os, "pathlib": pathlib, "fcntl": T.fcntl, "json": T.json,
    "time": T.time, "Any": object, "store_private_jsonl": getattr(T, "store_private_jsonl", None),
    "DEFAULT_MESSAGE_RETENTION_SECONDS": 7 * 24 * 60 * 60,
}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)


class PrivateFallbackMessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.path = self.root / "voice" / "messages.jsonl"
        self.env = mock.patch.dict(os.environ, {"MODEL_GARDEN_MESSAGE_RETENTION_SECONDS": "604800"})
        self.env.start()
        self.addCleanup(self.env.stop)
        namespace["MESSAGE_PATH"] = self.path

    def append(self):
        namespace["_append_message"]({"type": "FallbackMessage", "timestamp": 1_800_000_000,
                                       "message": "synthetic private message"})

    def test_symlink_target_is_never_modified(self):
        self.path.parent.mkdir()
        target = self.root / "other-client.jsonl"
        target.write_text("untouched\n")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.append()
        self.assertEqual(target.read_text(), "untouched\n")

    def test_hardlink_is_rejected_before_chmod_or_truncate(self):
        self.path.parent.mkdir()
        target = self.root / "other-client.jsonl"
        target.write_text("untouched\n")
        mode = stat.S_IMODE(target.stat().st_mode)
        os.link(target, self.path)
        with self.assertRaises(OSError):
            self.append()
        self.assertEqual(target.read_text(), "untouched\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)

    def test_parent_symlink_does_not_create_or_chmod_external_directory(self):
        target = self.root / "other-client"
        target.mkdir(mode=0o755)
        self.path.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            self.append()
        self.assertFalse((target / self.path.name).exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_fifo_fails_without_blocking(self):
        self.path.parent.mkdir()
        os.mkfifo(self.path)
        with self.assertRaises(OSError):
            self.append()

    def test_file_replacement_while_waiting_for_lock_fails_closed(self):
        self.path.parent.mkdir()
        self.path.write_text("original\n")
        target = self.root / "other-client.jsonl"
        target.write_text("untouched\n")
        flock = T.fcntl.flock
        replaced = False

        def replace(fd, op):
            nonlocal replaced
            result = flock(fd, op)
            if op == T.fcntl.LOCK_EX and not replaced:
                replaced = True
                self.path.unlink()
                self.path.symlink_to(target)
            return result

        with mock.patch.object(T.fcntl, "flock", side_effect=replace):
            with self.assertRaises(OSError):
                self.append()
        self.assertTrue(replaced)
        self.assertEqual(target.read_text(), "untouched\n")

    def test_parent_directory_replacement_while_locked_fails_closed(self):
        self.path.parent.mkdir()
        self.path.write_text("original\n")
        flock = T.fcntl.flock
        replaced = False

        def replace(fd, op):
            nonlocal replaced
            result = flock(fd, op)
            if op == T.fcntl.LOCK_EX and not replaced:
                replaced = True
                self.path.parent.rename(self.root / "detached")
                self.path.parent.mkdir()
            return result

        with mock.patch.object(T.fcntl, "flock", side_effect=replace):
            with self.assertRaises(OSError):
                self.append()
        self.assertTrue(replaced)
        self.assertEqual((self.root / "detached" / self.path.name).read_text(), "original\n")
        self.assertFalse(self.path.exists())

    def test_retention_zero_clears_existing_messages_and_does_not_store_new_ones(self):
        self.path.parent.mkdir()
        self.path.write_text('{"timestamp":1800000000,"message":"old"}\n')
        with mock.patch.dict(os.environ, {"MODEL_GARDEN_MESSAGE_RETENTION_SECONDS": "0"}):
            self.append()
        self.assertEqual(self.path.read_text(), "")

    def test_existing_owned_regular_file_keeps_bounded_retention_and_private_modes(self):
        self.path.parent.mkdir()
        self.path.write_text('{"timestamp":1,"message":"expired"}\n')
        with mock.patch.object(T.time, "time", return_value=1_800_000_001):
            self.append()
        self.assertNotIn("expired", self.path.read_text())
        self.assertIn("synthetic private message", self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
