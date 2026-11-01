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
    def test_new_workspace_pause_resume_and_restart_are_read_from_shared_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with mock.patch.object(voice_pause, "PAUSE_ROOT", root):
                self.assertFalse(voice_pause.paused())
                voice_pause.PauseStore(root).pause("owner@client.example", "incident", 1800000000)
                self.assertTrue(voice_pause.paused())
                self.assertTrue(voice_pause.paused())  # independent reader, no process cache
                voice_pause.PauseStore(root).resume("owner@client.example", 1800000001)
                self.assertFalse(voice_pause.paused())

    def test_unavailable_or_tampered_pause_mount_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with mock.patch.object(voice_pause, "PAUSE_ROOT", root / "missing"):
                self.assertTrue(voice_pause.paused())
            state = root / "pause.json"
            state.write_text('{"paused": false}', encoding="utf-8")
            state.chmod(0o644)
            with mock.patch.object(voice_pause, "PAUSE_ROOT", root):
                self.assertTrue(voice_pause.paused())
            state.unlink()
            state.symlink_to(root / "missing")
            with mock.patch.object(voice_pause, "PAUSE_ROOT", root):
                self.assertTrue(voice_pause.paused())

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

    def test_voice_overlay_mounts_shared_governance_read_only_and_bundles_pause_reader(self):
        source = (ROOT / "scripts/deploy-reference-voice.py").read_text()
        self.assertIn("governance-data:/opt/governance:ro", source)
        self.assertIn("voice_pause.py", source)
        self.assertIn("pause.py", source)
        self.assertIn("governed-tools", source)


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
