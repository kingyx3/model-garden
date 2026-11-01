"""Fail-closed fallback-message storage regressions for P0 voice privacy (#107).

Pure Python: extract the transport-independent writer from the LiveKit bridge;
no provider credentials or LiveKit runtime are required.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import importlib.util
import os
import pathlib
import re
import stat
import sys
import tempfile
from types import SimpleNamespace
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
             and node.name in {"_retention_seconds", "_append_message"}]
assert len(functions) == 2
namespace = {
    "os": os, "pathlib": pathlib, "fcntl": T.fcntl, "json": T.json,
    "time": T.time, "Any": object, "store_private_jsonl": getattr(T, "store_private_jsonl", None),
    "DEFAULT_MESSAGE_RETENTION_SECONDS": 7 * 24 * 60 * 60,
}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)

# Compile the voice Tool handler against minimal provider stubs so failure
# responses are exercised without LiveKit, SIP or an actual caller.
class _Agent:
    def __init__(self, instructions):
        self.instructions = instructions


async def _unpaused_fixture():
    # This fixture tests private storage, not the independent owner pause
    # admission path (covered in test_voice_pause.py).
    return False


namespace.update({
    "Agent": _Agent, "CallTranscript": T.CallTranscript, "RunContext": object,
    "function_tool": lambda: lambda fn: fn,
    "get_job_context": lambda: SimpleNamespace(room=SimpleNamespace(name="synthetic-room")),
    "_sip_participant": lambda ctx: None, "_safe_digest": lambda value: "digest",
    "LOGGER": logging.getLogger("voice-fallback-test"),
    "AGENT_NAME": "model-garden-receptionist",
    "voice_paused": _unpaused_fixture,
    "redact": T.redact,
    "TRANSCRIPT_CONFIG": T.TranscriptConfig(),
    "MAX_FALLBACK_MESSAGE_CHARS": 4000,
    "MAX_FALLBACK_NAME_CHARS": 200,
})
agent_node = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == "ReceptionistVoiceAgent")
exec(compile(ast.Module(body=[agent_node], type_ignores=[]), str(source), "exec"), namespace)


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
        return namespace["_append_message"]({"type": "FallbackMessage", "timestamp": 1_800_000_000,
                                       "message": "synthetic private message"})

    def seed(self, text: str) -> None:
        """Fixture simulates an existing owner-private evidence file."""
        self.path.write_text(text)
        self.path.chmod(0o600)

    def test_symlink_target_is_never_modified(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.root / "other-client.jsonl"
        target.write_text("untouched\n")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.append()
        self.assertEqual(target.read_text(), "untouched\n")

    def test_hardlink_is_rejected_before_chmod_or_truncate(self):
        self.path.parent.mkdir(mode=0o700)
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
        self.path.parent.mkdir(mode=0o700)
        os.mkfifo(self.path)
        with self.assertRaises(OSError):
            self.append()

    def test_file_replacement_while_waiting_for_lock_fails_closed(self):
        self.path.parent.mkdir(mode=0o700)
        self.seed("original\n")
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
        self.path.parent.mkdir(mode=0o700)
        self.seed("original\n")
        flock = T.fcntl.flock
        replaced = False

        def replace(fd, op):
            nonlocal replaced
            result = flock(fd, op)
            if op == T.fcntl.LOCK_EX and not replaced:
                replaced = True
                self.path.parent.rename(self.root / "detached")
                self.path.parent.mkdir(mode=0o700)
            return result

        with mock.patch.object(T.fcntl, "flock", side_effect=replace):
            with self.assertRaises(OSError):
                self.append()
        self.assertTrue(replaced)
        self.assertEqual((self.root / "detached" / self.path.name).read_text(), "original\n")
        self.assertFalse(self.path.exists())

    def test_retention_zero_clears_existing_messages_and_does_not_store_new_ones(self):
        self.path.parent.mkdir(mode=0o700)
        self.seed('{"timestamp":1800000000,"message":"old"}\n')
        with mock.patch.dict(os.environ, {"MODEL_GARDEN_MESSAGE_RETENTION_SECONDS": "0"}):
            self.assertFalse(self.append())
        self.assertEqual(self.path.read_text(), "")

    def test_message_capture_does_not_claim_success_when_retention_is_disabled(self):
        class Transcript:
            outcomes = []
            def note_outcome(self, value):
                self.outcomes.append(value)

        transcript = Transcript()
        agent = namespace["ReceptionistVoiceAgent"]("test", transcript)
        with mock.patch.dict(os.environ, {"MODEL_GARDEN_MESSAGE_RETENTION_SECONDS": "0"}):
            result = asyncio.run(agent.capture_message(None, "synthetic request"))
        self.assertIn("no message was saved", result)
        self.assertEqual(transcript.outcomes, [])
        self.assertEqual(self.path.read_text(), "")

    def test_saved_fallback_message_records_its_emitting_agent(self):
        agent = namespace["ReceptionistVoiceAgent"]("test")
        result = asyncio.run(agent.capture_message(None, "synthetic private request"))
        self.assertIn("captured", result)
        (row,) = [T.json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(row["agent"], namespace["AGENT_NAME"])

    def test_message_capture_does_not_claim_success_when_private_store_is_unsafe(self):
        target = self.root / "other-client.jsonl"
        target.write_text("untouched\\n")
        self.path.parent.mkdir(mode=0o700)
        self.path.symlink_to(target)
        agent = namespace["ReceptionistVoiceAgent"]("test")
        with self.assertLogs("voice-fallback-test", level="ERROR") as logs:
            result = asyncio.run(agent.capture_message(None, "synthetic private request"))
        self.assertIn("could not be saved safely", result)
        self.assertEqual(target.read_text(), "untouched\\n")
        self.assertNotIn("synthetic private request", str(logs.output))

    def test_fallback_redacts_cards_and_client_patterns_even_with_transcripts_disabled(self):
        # Fallback messages have their own retention path; disabling the
        # transcript must not turn off the configured privacy redaction.
        previous = namespace["TRANSCRIPT_CONFIG"]
        namespace["TRANSCRIPT_CONFIG"] = T.TranscriptConfig(
            enabled=False, patterns=(re.compile(r"\\bVIP-\\d{4}\\b"),)
        )
        try:
            agent = namespace["ReceptionistVoiceAgent"]("test")
            result = asyncio.run(agent.capture_message(
                None, "Please call me about card 4111 1111 1111 1111 and VIP-1234",
                caller_name="VIP-4321",
            ))
        finally:
            namespace["TRANSCRIPT_CONFIG"] = previous
        self.assertIn("captured", result)
        (row,) = [T.json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(row["message"], "Please call me about card [REDACTED:card] and [REDACTED:pattern]")
        self.assertEqual(row["caller_name"], "[REDACTED:pattern]")
        self.assertEqual(row["redactions"], {"card": 1, "pattern": 2})
        self.assertNotIn("4111", self.path.read_text())
        self.assertNotIn("VIP-", self.path.read_text())

    def test_non_string_tool_arguments_are_refused_before_any_write(self):
        agent = namespace["ReceptionistVoiceAgent"]("test")
        for message, name in ((None, ""), ({"private": "value"}, ""), ("safe", None), ("safe", ["name"])):
            with self.subTest(message_type=type(message).__name__, name_type=type(name).__name__):
                result = asyncio.run(agent.capture_message(None, message, caller_name=name))
                self.assertIn("invalid", result.lower())
                self.assertFalse(self.path.exists())

    def test_oversized_fallback_message_or_name_is_refused_without_persistence(self):
        agent = namespace["ReceptionistVoiceAgent"]("test")
        for message, name in (("x" * 4001, ""), ("safe", "y" * 201)):
            with self.subTest(message_length=len(message), name_length=len(name)):
                result = asyncio.run(agent.capture_message(None, message, caller_name=name))
                self.assertIn("too long", result)
                self.assertFalse(self.path.exists(), "rejected fallback must not write a receipt or payload")

    def test_existing_owned_regular_file_keeps_bounded_retention_and_private_modes(self):
        self.path.parent.mkdir(mode=0o700)
        self.seed('{"timestamp":1,"message":"expired"}\n')
        with mock.patch.object(T.time, "time", return_value=1_800_000_001):
            self.assertTrue(self.append())
        self.assertNotIn("expired", self.path.read_text())
        self.assertIn("synthetic private message", self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
