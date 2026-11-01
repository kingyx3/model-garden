"""SIP hangup must flush partial LiveKit turns before voice evidence shutdown (#107).

The pinned LiveKit SDK normally closes on disconnect; explicitly closing the
session for the last SIP caller covers a room that remains alive with observers.
These are event/transport fakes, not a live SIP or TTS-completeness claim.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
from types import SimpleNamespace
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "platform/channels/livekit_receptionist.py"


def load_registration():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_register_sip_disconnect_flush")
    namespace = {
        "asyncio": asyncio,
        "rtc": SimpleNamespace(ParticipantKind=SimpleNamespace(SIP="sip")),
        "LOGGER": logging.getLogger("test.voice-disconnect"),
    }
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        function,
    ], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["_register_sip_disconnect_flush"]


class Room:
    def __init__(self, participants=None):
        self.remote_participants = participants or {}
        self.handlers = {}

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback
        return register

    def disconnect(self, participant):
        self.handlers["participant_disconnected"](participant)


def person(identity, kind="sip"):
    return SimpleNamespace(identity=identity, kind=kind)


class Session:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    async def aclose(self):
        self.calls += 1
        if self.error:
            raise self.error


class VoiceSipDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_sip_hangup_flushes_once_even_with_non_sip_observer(self):
        register = load_registration()
        caller, observer = person("sip-caller"), person("observer", "standard")
        room = Room({"observer": observer})
        session = Session()
        register(SimpleNamespace(room=room), session)
        room.disconnect(caller)
        room.disconnect(caller)
        await asyncio.sleep(0.01)
        self.assertEqual(session.calls, 1)

    async def test_other_sip_caller_prevents_closing_ongoing_call(self):
        register = load_registration()
        caller, other = person("caller"), person("other")
        room = Room({"other": other})
        session = Session()
        register(SimpleNamespace(room=room), session)
        room.disconnect(caller)
        await asyncio.sleep(0.01)
        self.assertEqual(session.calls, 0)
        room.remote_participants.clear()
        room.disconnect(other)
        await asyncio.sleep(0.01)
        self.assertEqual(session.calls, 1)

    async def test_non_sip_and_untrusted_identity_never_close_session(self):
        register = load_registration()
        room, session = Room(), Session()
        register(SimpleNamespace(room=room), session)
        for participant in (person("observer", "standard"), person(""), person(None)):
            room.disconnect(participant)
        await asyncio.sleep(0.01)
        self.assertEqual(session.calls, 0)

    async def test_close_failure_is_redacted_and_does_not_escape_callback(self):
        register = load_registration()
        room = Room()
        session = Session(RuntimeError("sensitive caller text"))
        register(SimpleNamespace(room=room), session)
        with self.assertLogs("test.voice-disconnect", level="ERROR") as logs:
            room.disconnect(person("caller"))
            await asyncio.sleep(0.01)
        self.assertEqual(session.calls, 1)
        self.assertIn("RuntimeError", str(logs.output))
        self.assertNotIn("sensitive caller text", str(logs.output))

    def test_registration_precedes_session_start_and_call_shutdown(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "receptionist")
        source = ast.unparse(handler)
        self.assertIn("_register_sip_disconnect_flush(ctx, session)", source)
        self.assertLess(source.index("_register_sip_disconnect_flush(ctx, session)"),
                        source.index("await session.start("))
        self.assertIn("ctx.add_shutdown_callback(on_transcript_shutdown)", source)


if __name__ == "__main__":
    unittest.main()
