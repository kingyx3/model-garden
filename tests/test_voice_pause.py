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
            self.assertFalse(asyncio.run(voice_pause.paused()))
            connection.assert_called_once_with("governed-tools", 9120, timeout=2)
            connection.return_value.request.assert_called_once_with("GET", "/internal/voice-pause")
            connection.return_value.close.assert_called_once()
        for status in (204, 302, 401, 423, 500):
            with self.subTest(status=status), mock.patch.object(voice_pause.http.client, "HTTPConnection") as connection:
                connection.return_value.getresponse.return_value.status = status
                self.assertTrue(asyncio.run(voice_pause.paused()))
                connection.return_value.close.assert_called_once()

    def test_sidecar_unavailable_or_malformed_http_fails_closed(self):
        for error in (TimeoutError("timeout"), OSError("connection refused"),
                      voice_pause.http.client.BadStatusLine("bad")):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                voice_pause.http.client, "HTTPConnection"
            ) as connection:
                connection.return_value.request.side_effect = error
                self.assertTrue(asyncio.run(voice_pause.paused()))
                connection.return_value.close.assert_called_once()

    def test_voice_entry_holds_without_model_generation_and_suppresses_transcript(self):
        tree = ast.parse((ROOT / "platform/channels/livekit_receptionist.py").read_text())
        agent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ReceptionistVoiceAgent")
        entry = next(n for n in agent.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_enter")
        source = ast.unparse(entry)
        self.assertIn("if await voice_paused():", source)
        self.assertIn("self._transcript.suppress()", source)
        self.assertIn("self.session.say(HOLDING", source)
        self.assertIn("self.session.generate_reply(", source)
        self.assertLess(source.index("if await voice_paused():"), source.index("self.session.generate_reply("))

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
                self.assertIn("if await voice_paused():", source)
                self.assertLess(source.index("if await voice_paused():"), source.index(side_effect))

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

        async def paused_true():
            return True

        namespace = {
            "Agent": BaseAgent, "function_tool": lambda: (lambda fn: fn),
            "HOLDING": voice_pause.HOLDING, "voice_paused": paused_true,
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
        async def paused_during_disclosure():
            return next(states)
        namespace["voice_paused"] = paused_during_disclosure
        transcript = Transcript()
        agent = cls("receptionist", transcript)
        asyncio.run(agent.on_enter())
        self.assertEqual(agent.session.spoken, [transcript.disclosure_text, voice_pause.HOLDING])
        self.assertEqual(agent.session.generated, [])
        self.assertTrue(transcript.suppressed)

        async def active():
            return False
        namespace["voice_paused"] = active
        transcript = Transcript()
        agent = cls("receptionist", transcript)
        asyncio.run(agent.on_enter())
        self.assertEqual(agent.session.spoken, [transcript.disclosure_text])
        self.assertEqual(len(agent.session.generated), 1)
        self.assertTrue(transcript.played)
        self.assertFalse(transcript.suppressed)

    def test_inflight_pause_monitor_closes_once_after_active_call(self):
        # Execute the actual asynchronous monitor; no LiveKit or SIP transport.
        async def scenario():
            states = iter((False, False, True))
            async def status():
                return next(states)
            session = mock.AsyncMock()
            with mock.patch.object(voice_pause, "paused", side_effect=status):
                await voice_pause.stop_session_on_pause(
                    session, interval_seconds=0
                )
            session.aclose.assert_awaited_once()

        asyncio.run(scenario())

    def test_inflight_pause_monitor_fails_closed_on_sidecar_error(self):
        async def scenario():
            async def unavailable():
                raise RuntimeError("sensitive provider details must not escape")
            session = mock.AsyncMock()
            with mock.patch.object(voice_pause, "paused", side_effect=unavailable):
                await voice_pause.stop_session_on_pause(
                    session, interval_seconds=0
                )
            session.aclose.assert_awaited_once()

        asyncio.run(scenario())

    def test_inflight_pause_close_retries_transient_sdk_failure_without_resuming(self):
        # A failed SDK close must not silently terminate the safety watcher.
        # A later active response cannot retract the already observed pause.
        async def scenario():
            session = mock.AsyncMock()
            session.aclose.side_effect = [RuntimeError("private caller data"), None]
            with mock.patch.object(voice_pause, "paused", side_effect=[True, False]) as status, \
                 mock.patch.object(voice_pause.LOGGER, "error") as logged:
                await voice_pause.stop_session_on_pause(session, interval_seconds=0)
            self.assertEqual(session.aclose.await_count, 2)
            self.assertEqual(status.await_count, 1)
            self.assertEqual(logged.call_count, 1)
            self.assertNotIn("private caller data", str(logged.call_args))

        asyncio.run(scenario())

    def test_inflight_pause_close_keeps_retrying_until_shutdown_cancellation(self):
        # A persistent close failure is not a successful emergency stop.
        async def scenario():
            attempts = 0
            enough = asyncio.Event()

            async def cannot_close():
                nonlocal attempts
                attempts += 1
                if attempts >= 3:
                    enough.set()
                raise RuntimeError("sensitive voice data")

            session = mock.AsyncMock()
            session.aclose.side_effect = cannot_close
            with mock.patch.object(voice_pause, "paused", return_value=True) as status, \
                 mock.patch.object(voice_pause.LOGGER, "error") as logged:
                task = asyncio.create_task(voice_pause.stop_session_on_pause(session, interval_seconds=0))
                await asyncio.wait_for(enough.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertGreaterEqual(attempts, 3)
            self.assertEqual(status.await_count, 1)
            self.assertNotIn("sensitive voice data", str(logged.call_args_list))

        asyncio.run(scenario())

    def test_inflight_pause_monitor_cancellation_does_not_close_active_session(self):
        async def scenario():
            entered = asyncio.Event()
            async def active():
                entered.set()
                return False
            session = mock.AsyncMock()
            with mock.patch.object(voice_pause, "paused", side_effect=active):
                task = asyncio.create_task(voice_pause.stop_session_on_pause(session))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            session.aclose.assert_not_awaited()

        asyncio.run(scenario())

    def test_voice_worker_registers_and_cancels_one_inflight_pause_watch(self):
        worker = (ROOT / "platform/channels/livekit_receptionist.py").read_text()
        self.assertIn("stop_session_on_pause(", worker)
        self.assertIn("monitor_task.cancel()", worker)
        self.assertIn("ctx.add_shutdown_callback(stop_pause_monitor)", worker)
        self.assertLess(worker.index("ctx.add_shutdown_callback(stop_pause_monitor)"),
                        worker.index("ctx.add_shutdown_callback(on_transcript_shutdown)"))
        self.assertIn("monitor_task is asyncio.current_task()", worker)
        self.assertNotIn("on_pause=transcript.suppress", worker)


if __name__ == "__main__":
    unittest.main()
