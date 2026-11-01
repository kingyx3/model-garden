"""Credential-free emergency voice pause regressions (#232).

The pure pause reader is exercised without SIP, model or provider credentials.
Static integration checks keep the LiveKit worker's pause checks in the
call-start and provider-side-effect paths. These are NOT live cancellation proof.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "core"))
spec = importlib.util.spec_from_file_location("voice_pause_under_test", ROOT / "platform" / "channels" / "voice_pause.py")
voice_pause = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = voice_pause
spec.loader.exec_module(voice_pause)


class VoicePauseTests(unittest.TestCase):
    def test_private_sidecar_active_pause_and_fail_closed_responses(self):
        with mock.patch.object(voice_pause.http.client, "HTTPConnection") as connection:
            connection.return_value.getresponse.return_value.status = 200
            self.assertFalse(voice_pause.paused())
            connection.assert_called_once_with("governed-tools", 9120, timeout=2)
            connection.return_value.request.assert_called_once_with("GET", "/internal/voice-pause")
            connection.return_value.close.assert_called_once()
        for status in (204, 302, 401, 423, 500):
            with self.subTest(status=status), mock.patch.object(voice_pause.http.client, "HTTPConnection") as connection:
                connection.return_value.getresponse.return_value.status = status
                self.assertTrue(voice_pause.paused())
                connection.return_value.close.assert_called_once()

    def test_sidecar_unavailable_or_malformed_http_fails_closed(self):
        for error in (TimeoutError("timeout"), OSError("connection refused"),
                      voice_pause.http.client.BadStatusLine("bad")):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                voice_pause.http.client, "HTTPConnection"
            ) as connection:
                connection.return_value.request.side_effect = error
                self.assertTrue(voice_pause.paused())
                connection.return_value.close.assert_called_once()

    def test_voice_entry_holds_without_model_generation_and_suppresses_transcript(self):
        tree = ast.parse((ROOT / "platform/channels/livekit_receptionist.py").read_text())
        agent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ReceptionistVoiceAgent")
        entry = next(n for n in agent.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_enter")
        source = ast.unparse(entry)
        self.assertIn("if voice_paused():", source)
        self.assertIn("self._transcript.suppress()", source)
        self.assertIn("self.session.say(HOLDING", source)
        self.assertIn("self.session.generate_reply(", source)
        self.assertLess(source.index("if voice_paused():"), source.index("self.session.generate_reply("))

    def test_voice_provider_actions_check_pause_before_side_effects(self):
        tree = ast.parse((ROOT / "platform/channels/livekit_receptionist.py").read_text())
        agent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ReceptionistVoiceAgent")
        for method, side_effect in (
            ("transfer_to_human", "transfer_sip_participant"),
            ("capture_message", "_append_message("),
        ):
            with self.subTest(method=method):
                node = next(n for n in agent.body if isinstance(n, ast.AsyncFunctionDef) and n.name == method)
                source = ast.unparse(node)
                self.assertIn("if voice_paused():", source)
                self.assertLess(source.index("if voice_paused():"), source.index(side_effect))

    def test_voice_overlay_does_not_expose_governance_volume_to_model_container(self):
        source = (ROOT / "scripts/deploy-reference-voice.py").read_text()
        self.assertNotIn("governance-data:/opt/governance", source)
        self.assertIn("voice_pause.py", source)
        self.assertIn("pause.py", source)
        self.assertIn("governed-tools", source)
        self.assertIn("governed-tools", (ROOT / "platform/channels/voice_pause.py").read_text())
        server = (ROOT / "platform/runtime/governed_mcp.py").read_text()
        self.assertIn('/internal/voice-pause', server)
        self.assertIn("custom_route", server)


    def test_paused_agent_speaks_holding_without_model_or_fallback_effects(self):
        # Execute the real worker class with transport stubs; no LiveKit import,
        # provider, SIP call, model or cloud credential is involved.
        tree = ast.parse((ROOT / "platform/channels/livekit_receptionist.py").read_text())
        agent_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ReceptionistVoiceAgent")

        class Session:
            def __init__(self):
                self.spoken = []
                self.generated = []

            async def say(self, text, **kwargs):
                self.spoken.append(text)

            def generate_reply(self, **kwargs):
                self.generated.append(kwargs)

        class BaseAgent:
            def __init__(self, *, instructions):
                self.session = Session()

        class Transcript:
            disclosure_text = "This call is transcribed."

            def __init__(self):
                self.suppressed = False
                self.played = False

            def suppress(self):
                self.suppressed = True

            def mark_disclosure_played(self):
                self.played = True

        namespace = {
            "Agent": BaseAgent, "function_tool": lambda: (lambda fn: fn),
            "HOLDING": voice_pause.HOLDING, "voice_paused": lambda: True,
        }
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            agent_node,
        ], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, "<voice-worker-agent>", "exec"), namespace)
        cls = namespace["ReceptionistVoiceAgent"]

        transcript = Transcript()
        agent = cls("receptionist", transcript, call_hash="a" * 64)
        asyncio.run(agent.on_enter())
        self.assertEqual(agent.session.spoken, [voice_pause.HOLDING])
        self.assertEqual(agent.session.generated, [])
        self.assertTrue(transcript.suppressed)
        self.assertFalse(transcript.played)
        # Neither provider nor fallback store is available in this test:
        # reaching either would raise, rather than returning the holding text.
        self.assertEqual(asyncio.run(agent.transfer_to_human(None)), voice_pause.HOLDING)
        self.assertEqual(asyncio.run(agent.capture_message(None, "private caller message")), voice_pause.HOLDING)

        # Pause committed while disclosure was being spoken also blocks
        # model generation; the unpaused path still generates normally.
        states = iter([False, True])
        namespace["voice_paused"] = lambda: next(states)
        transcript = Transcript()
        agent = cls("receptionist", transcript)
        asyncio.run(agent.on_enter())
        self.assertEqual(agent.session.spoken, [transcript.disclosure_text, voice_pause.HOLDING])
        self.assertEqual(agent.session.generated, [])
        self.assertTrue(transcript.suppressed)

        namespace["voice_paused"] = lambda: False
        transcript = Transcript()
        agent = cls("receptionist", transcript)
        asyncio.run(agent.on_enter())
        self.assertEqual(agent.session.spoken, [transcript.disclosure_text])
        self.assertEqual(len(agent.session.generated), 1)
        self.assertTrue(transcript.played)
        self.assertFalse(transcript.suppressed)

if __name__ == "__main__":
    unittest.main()
