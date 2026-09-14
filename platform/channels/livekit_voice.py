"""Minimal LiveKit voice channel adapter for the Model Garden Receptionist MVP.

LiveKit owns SIP/media transport and realtime voice mechanics. Model Garden only
normalizes provider events into its small channel contract and keeps business Agent
configuration provider-neutral. LiveKit credentials and transfer APIs are injected
at runtime rather than stored in client desired state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

TransferCall = Callable[[str, str], dict[str, Any] | None]
EventSink = Callable[[dict[str, Any]], None]


class LiveKitVoiceAdapter:
    """Translate LiveKit SIP participant state into Model Garden call events."""

    def __init__(
        self,
        *,
        tenant: str,
        receptionist: str,
        transfer_call: TransferCall | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant must be a non-empty string")
        if not isinstance(receptionist, str) or not receptionist.strip():
            raise ValueError("receptionist must be a non-empty string")
        self.tenant = tenant.strip()
        self.receptionist = receptionist.strip()
        self._transfer_call = transfer_call
        self._event_sink = event_sink

    @staticmethod
    def _required_attribute(attributes: dict[str, str], name: str) -> str:
        value = attributes.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"LiveKit SIP participant is missing required attribute {name}")
        return value.strip()

    def call_started(
        self,
        *,
        participant_identity: str,
        room_name: str,
        attributes: dict[str, str],
        locale: str | None = None,
    ) -> dict[str, Any]:
        """Create the canonical CallStarted event from an inbound SIP participant.

        LiveKit documents sip.callID as its per-call identifier, sip.phoneNumber as
        the inbound caller number when phone-number hiding is disabled, and
        sip.trunkPhoneNumber as the number dialed by the caller.
        """
        if not isinstance(participant_identity, str) or not participant_identity.strip():
            raise ValueError("participant_identity must be a non-empty string")
        if not isinstance(room_name, str) or not room_name.strip():
            raise ValueError("room_name must be a non-empty string")
        if not isinstance(attributes, dict):
            raise ValueError("attributes must be a mapping")

        trace_id = self._required_attribute(attributes, "sip.callID")
        called_number = self._required_attribute(attributes, "sip.trunkPhoneNumber")
        caller = attributes.get("sip.phoneNumber")
        if caller is not None and (not isinstance(caller, str) or not caller.strip()):
            caller = None

        event: dict[str, Any] = {
            "type": "CallStarted",
            "tenant": self.tenant,
            "receptionist": self.receptionist,
            "caller": caller.strip() if isinstance(caller, str) else None,
            "called_number": called_number,
            "trace_id": trace_id,
            "provider": "livekit",
            "provider_call_id": attributes.get("sip.callIDFull", trace_id),
            "provider_participant": participant_identity.strip(),
            "provider_room": room_name.strip(),
        }
        if locale is not None:
            if not isinstance(locale, str) or not locale.strip():
                raise ValueError("locale must be a non-empty string when supplied")
            event["locale"] = locale.strip()

        if self._event_sink:
            self._event_sink(event)
        return event

    def call_ended(
        self,
        *,
        trace_id: str,
        outcome: str,
        summary: str,
        actions: list[dict[str, Any]] | None = None,
        transfer_target: str | None = None,
    ) -> dict[str, Any]:
        """Create the canonical CallEnded event without provider-specific leakage."""
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError("outcome must be a non-empty string")
        if not isinstance(summary, str):
            raise ValueError("summary must be a string")
        if actions is not None and not isinstance(actions, list):
            raise ValueError("actions must be a list when supplied")

        event: dict[str, Any] = {
            "type": "CallEnded",
            "trace_id": trace_id.strip(),
            "outcome": outcome.strip(),
            "summary": summary,
            "actions": actions or [],
        }
        if transfer_target is not None:
            if not isinstance(transfer_target, str) or not transfer_target.strip():
                raise ValueError("transfer_target must be non-empty when supplied")
            event["transfer_target"] = transfer_target.strip()

        if self._event_sink:
            self._event_sink(event)
        return event

    def transfer(self, *, participant_identity: str, target_uri: str) -> dict[str, Any]:
        """Request a cold transfer through an injected LiveKit transfer operation.

        The adapter does not hold LiveKit credentials. Production code should inject
        a callable backed by LiveKit's TransferSIPParticipant API.
        """
        if not isinstance(participant_identity, str) or not participant_identity.strip():
            raise ValueError("participant_identity must be a non-empty string")
        if not isinstance(target_uri, str) or not target_uri.strip():
            raise ValueError("target_uri must be a non-empty string")
        if self._transfer_call is None:
            raise RuntimeError("LiveKit transfer operation is not configured")

        result = self._transfer_call(participant_identity.strip(), target_uri.strip())
        return {
            "provider": "livekit",
            "participant": participant_identity.strip(),
            "target": target_uri.strip(),
            "status": "requested",
            "provider_result": result,
        }
