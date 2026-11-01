"""Call-scoped Hermes session isolation for the pinned LiveKit/OpenAI bridge.

The pinned Hermes 0.21.2 Chat Completions route derives a headerless session
from system prompt + first user message. Two callers can say the same thing.
These credential-free tests require a distinct, opaque, explicitly supplied
session per LiveKit invocation; they do not attest governed Tool correlation.
"""
from __future__ import annotations

import ast
import pathlib
import re
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKER = ROOT / "platform" / "channels" / "livekit_receptionist.py"


def _session_builder():
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    builder = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "build_session")
    calls = []

    def capture(kind):
        def make(**kwargs):
            calls.append((kind, kwargs))
            return {"kind": kind, **kwargs}
        return make

    scope = {
        "AgentSession": capture("session"),
        "openai": SimpleNamespace(STT=capture("stt"), LLM=capture("llm"), TTS=capture("tts")),
        "_required": lambda name: {
            "OPENAI_API_KEY": "voice-test-key",
            "MODEL_GARDEN_HERMES_API_URL": "http://hermes:8642/v1",
            "MODEL_GARDEN_HERMES_API_KEY": "internal-test-key",
        }[name],
        "re": re,
    }
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), builder],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, str(WORKER), "exec"), scope)
    return scope["build_session"], calls


class HermesVoiceSessionTests(unittest.TestCase):
    def test_each_call_has_a_distinct_explicit_hermes_session(self):
        build, calls = _session_builder()
        first = build(call_hash="a" * 64)
        second = build(call_hash="b" * 64)
        one = first["llm"]["extra_headers"]
        two = second["llm"]["extra_headers"]
        self.assertEqual(one, {"X-Hermes-Session-Id": "voice-" + "a" * 64})
        self.assertEqual(two, {"X-Hermes-Session-Id": "voice-" + "b" * 64})
        self.assertNotEqual(one, two)
        self.assertEqual(first["llm"]["base_url"], "http://hermes:8642/v1")
        self.assertEqual(first["llm"]["api_key"], "internal-test-key")
        self.assertEqual(len([kind for kind, _ in calls if kind == "llm"]), 2)

    def test_invalid_or_untrusted_call_ids_fail_before_any_model_initialization(self):
        build, calls = _session_builder()
        for value in ("", "a" * 63, "A" * 64, "a" * 64 + "\r\nX-Forged: yes", "caller-id", None):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                build(call_hash=value)
        self.assertEqual(calls, [])

    def test_receptionist_uses_its_random_per_invocation_hash_not_room_or_caller(self):
        tree = ast.parse(WORKER.read_text(encoding="utf-8"))
        worker = next(node for node in tree.body
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == "receptionist")
        source = ast.unparse(worker)
        self.assertIn("call_hash = _call_hash(ctx.room.name)", source)
        self.assertIn("session = build_session(call_hash=call_hash)", source)
        # A session is for one call, not the reusable room, caller number or Agent name.
        self.assertNotIn("build_session(call_hash=room_hash)", source)
        self.assertNotIn("build_session(call_hash=AGENT_NAME)", source)


if __name__ == "__main__":
    unittest.main()
