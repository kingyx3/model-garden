"""Credential-free transfer destination safety regressions for #107/#117.

No LiveKit, SIP provider, tenant credential, or external transfer is used.
The deploy-time and call-time gates must agree before any SIP side effect.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET_PATH = ROOT / "platform" / "channels" / "transfer_target.py"
DEPLOY_PATH = ROOT / "scripts" / "deploy-reference-voice.py"
WORKER_PATH = ROOT / "platform" / "channels" / "livekit_receptionist.py"


def load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TARGET = load(TARGET_PATH, "transfer_target_test")
DEPLOY = load(DEPLOY_PATH, "transfer_deploy_test")


def binding(target):
    return {"spec": {"channels": {"voice": {
        "provider": "livekit",
        "url": "wss://voice.example.test",
        "api_key_ref": "secret://voice/key",
        "api_secret_ref": "secret://voice/secret",
        "human_transfer_target": target,
    }}}}


class TransferTargetTests(unittest.TestCase):
    def test_valid_explicit_destinations_pass_both_gates(self):
        for destination in (
            "tel:+15551234567",
            "tel:+441234567890",
            "sip:human@example.com",
            "sip:operator+desk@pbx.example.com:5061;transport=tls",
        ):
            with self.subTest(destination=destination):
                self.assertTrue(TARGET.valid_transfer_target(destination))
                self.assertEqual(DEPLOY._voice(binding(destination))["human_transfer_target"], destination)

    def test_invalid_or_ambiguous_destinations_are_refused_before_deploy(self):
        for destination in (
            "", "tel:", "tel:1234", "tel:+0", "tel:+1 555 1234",
            "tel:+15551234567;ext=9", "tel:+15551234567?x=y",
            "tel:+15551234567\r\nX-Header: injected",
            "tel:+15551234567\x00evil", "tel:+15551234567 ",
            " sip:human@example.com", "sip:", "sip:human", "sip:human@",
            "sip:human@bad..example", "sip:human@example.com;method=INVITE",
            "sip:human@example.com?subject=private", "sip:human@example.com#frag",
            "sip:human@example.com:65536", "sip:human@example.com:0",
            "sip:human:secret@example.com", "sips:human@example.com",
            "http://example.com", 42, "sip:" + "x" * 300,
        ):
            with self.subTest(destination=repr(destination)):
                self.assertFalse(TARGET.valid_transfer_target(destination))
                with self.assertRaisesRegex(ValueError, "human_transfer_target"):
                    DEPLOY._voice(binding(destination))

    def test_unconfigured_transfer_remains_optional(self):
        self.assertIsNone(DEPLOY._voice(binding(None))["human_transfer_target"])
        self.assertNotIn("human_transfer_target", DEPLOY._voice({"spec": {"channels": {"voice": {
            "provider": "livekit", "url": "wss://voice.example.test",
            "api_key_ref": "secret://voice/key", "api_secret_ref": "secret://voice/secret",
        }}}}))

    def test_runtime_rejects_malformed_target_without_sip_or_message_effect(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        agent = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "ReceptionistVoiceAgent")
        class BaseAgent:
            def __init__(self, *, instructions):
                pass

        async def unpaused():
            return False

        def forbidden_context():
            raise AssertionError("invalid target must not inspect SIP participants")

        scope = {
            "Agent": BaseAgent, "function_tool": lambda: (lambda fn: fn),
            "RunContext": object, "CallTranscript": object,
            "voice_paused": unpaused, "HOLDING": "paused",
            "valid_transfer_target": TARGET.valid_transfer_target,
            "os": os, "get_job_context": forbidden_context,
        }
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            agent,
        ], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(WORKER_PATH), "exec"), scope)
        receptionist = scope["ReceptionistVoiceAgent"]("test")
        for destination in ("tel:+15551234567\r\nX: 1", "sip:human@example.com?x=1", "tel:123"):
            with self.subTest(destination=destination), mock.patch.dict(
                os.environ, {"MODEL_GARDEN_HUMAN_TRANSFER_TARGET": destination}
            ):
                self.assertIn("invalid", asyncio.run(receptionist.transfer_to_human(None)).lower())

    def test_voice_build_context_contains_the_same_validator(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = DEPLOY._context(pathlib.Path(temporary))
            self.assertEqual((context / "transfer_target.py").read_bytes(), TARGET_PATH.read_bytes())
            dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("COPY transfer_target.py /opt/transfer_target.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
