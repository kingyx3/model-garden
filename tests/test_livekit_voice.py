from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHANNEL_PATH = ROOT / "platform" / "channels" / "livekit_voice.py"

channel_spec = importlib.util.spec_from_file_location("livekit_voice", CHANNEL_PATH)
voice = importlib.util.module_from_spec(channel_spec)
assert channel_spec.loader is not None
channel_spec.loader.exec_module(voice)


class LiveKitVoiceAdapterTests(unittest.TestCase):
    def test_normalizes_inbound_sip_participant(self):
        events = []
        adapter = voice.LiveKitVoiceAdapter(
            tenant="acme",
            receptionist="front-desk",
            event_sink=events.append,
        )

        event = adapter.call_started(
            participant_identity="sip_123",
            room_name="call-random",
            locale="en-SG",
            attributes={
                "sip.callID": "SCL_123",
                "sip.callIDFull": "provider-call-id",
                "sip.phoneNumber": "+6588880000",
                "sip.trunkPhoneNumber": "+6565550100",
                "sip.trunkID": "ST_123",
                "sip.ruleID": "SDR_123",
            },
        )

        self.assertEqual(event["type"], "CallStarted")
        self.assertEqual(event["tenant"], "acme")
        self.assertEqual(event["receptionist"], "front-desk")
        self.assertEqual(event["caller"], "+6588880000")
        self.assertEqual(event["called_number"], "+6565550100")
        self.assertEqual(event["trace_id"], "SCL_123")
        self.assertEqual(event["provider_call_id"], "provider-call-id")
        self.assertEqual(event["provider_participant"], "sip_123")
        self.assertEqual(event["provider_room"], "call-random")
        self.assertEqual(event["locale"], "en-SG")
        self.assertEqual(events, [event])

    def test_allows_hidden_caller_number(self):
        adapter = voice.LiveKitVoiceAdapter(tenant="acme", receptionist="front-desk")
        event = adapter.call_started(
            participant_identity="sip_123",
            room_name="call-random",
            attributes={
                "sip.callID": "SCL_123",
                "sip.trunkPhoneNumber": "+6565550100",
            },
        )
        self.assertIsNone(event["caller"])

    def test_requires_stable_trace_and_called_number(self):
        adapter = voice.LiveKitVoiceAdapter(tenant="acme", receptionist="front-desk")
        with self.assertRaisesRegex(ValueError, "sip.callID"):
            adapter.call_started(
                participant_identity="sip_123",
                room_name="call-random",
                attributes={"sip.trunkPhoneNumber": "+6565550100"},
            )
        with self.assertRaisesRegex(ValueError, "sip.trunkPhoneNumber"):
            adapter.call_started(
                participant_identity="sip_123",
                room_name="call-random",
                attributes={"sip.callID": "SCL_123"},
            )

    def test_transfer_is_runtime_injected(self):
        calls = []

        def transfer(participant, target):
            calls.append((participant, target))
            return {"ok": True}

        adapter = voice.LiveKitVoiceAdapter(
            tenant="acme",
            receptionist="front-desk",
            transfer_call=transfer,
        )
        result = adapter.transfer(
            participant_identity="sip_123",
            target_uri="tel:+6565550199",
        )

        self.assertEqual(calls, [("sip_123", "tel:+6565550199")])
        self.assertEqual(result["provider"], "livekit")
        self.assertEqual(result["status"], "requested")
        self.assertEqual(result["provider_result"], {"ok": True})

    def test_transfer_fails_closed_without_runtime_operation(self):
        adapter = voice.LiveKitVoiceAdapter(tenant="acme", receptionist="front-desk")
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            adapter.transfer(participant_identity="sip_123", target_uri="tel:+6565550199")

    def test_call_ended_is_provider_neutral_and_emitted(self):
        events = []
        adapter = voice.LiveKitVoiceAdapter(
            tenant="acme",
            receptionist="front-desk",
            event_sink=events.append,
        )
        event = adapter.call_ended(
            trace_id="SCL_123",
            outcome="transferred",
            summary="Caller requested the on-call technician.",
            actions=[{"tool": "human.transfer", "result": "requested"}],
            transfer_target="tel:+6565550199",
        )

        self.assertEqual(
            event,
            {
                "type": "CallEnded",
                "trace_id": "SCL_123",
                "outcome": "transferred",
                "summary": "Caller requested the on-call technician.",
                "actions": [{"tool": "human.transfer", "result": "requested"}],
                "transfer_target": "tel:+6565550199",
            },
        )
        self.assertEqual(events, [event])


if __name__ == "__main__":
    unittest.main()
