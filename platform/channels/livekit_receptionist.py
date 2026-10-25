"""LiveKit voice worker for the production-shaped Receptionist reference path.

LiveKit owns SIP/media transport, STT/TTS, and call transfer. Hermes remains the
reasoning/runtime authority through its authenticated OpenAI-compatible API. This
worker adds only the thin channel bridge plus deterministic human-transfer/message-
capture fallbacks required for the reference employee.

Required environment variables:
- LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
- OPENAI_API_KEY (voice STT/TTS credential)
- MODEL_GARDEN_HERMES_API_URL (normally http://hermes:8642/v1)
- MODEL_GARDEN_HERMES_API_KEY (container-internal bearer token)
Optional:
- MODEL_GARDEN_LIVEKIT_AGENT_NAME (default model-garden-receptionist)
- MODEL_GARDEN_HUMAN_TRANSFER_TARGET (tel:+... or sip:...)
- MODEL_GARDEN_MESSAGE_PATH (default /opt/data/messages.jsonl)
- MODEL_GARDEN_MESSAGE_RETENTION_SECONDS (default 604800 / 7 days)
- MODEL_GARDEN_TRANSCRIPT* (two-way call transcript, disclosure, redaction and retention; see call_transcript.py)
"""
from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import time
from typing import Any

from livekit import api, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool, get_job_context
from livekit.plugins import openai

from call_transcript import CallTranscript, config_from_env as transcript_config_from_env, finalize as finalize_transcript, store_private_jsonl

LOGGER = logging.getLogger("model-garden.receptionist")
# An unsafe transcript configuration (for example no disclosure and no lawful-basis owner) stops the worker at start.
TRANSCRIPT_CONFIG = transcript_config_from_env()

MESSAGE_PATH = pathlib.Path(os.environ.get("MODEL_GARDEN_MESSAGE_PATH", "/opt/data/messages.jsonl"))
AGENT_NAME = os.environ.get("MODEL_GARDEN_LIVEKIT_AGENT_NAME", "model-garden-receptionist").strip() or "model-garden-receptionist"
DEFAULT_MESSAGE_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _retention_seconds() -> int:
    raw = os.environ.get("MODEL_GARDEN_MESSAGE_RETENTION_SECONDS", str(DEFAULT_MESSAGE_RETENTION_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("MODEL_GARDEN_MESSAGE_RETENTION_SECONDS must be an integer") from exc
    if value < 0 or value > 365 * 24 * 60 * 60:
        raise RuntimeError("MODEL_GARDEN_MESSAGE_RETENTION_SECONDS must be between 0 and 31536000")
    return value


def _safe_digest(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _append_message(payload: dict[str, Any]) -> bool:
    """Store private fallback/lifecycle records; report whether retention allowed saving."""
    retention = _retention_seconds()
    store_private_jsonl(MESSAGE_PATH, payload, retention_seconds=retention)
    return retention > 0


def _sip_participant(ctx: JobContext):
    for participant in ctx.room.remote_participants.values():
        if participant.kind == rtc.ParticipantKind.SIP:
            return participant
    return None


class ReceptionistVoiceAgent(Agent):
    def __init__(self, instructions: str, transcript: CallTranscript | None = None) -> None:
        super().__init__(instructions=instructions)
        self._transcript = transcript

    async def on_enter(self) -> None:
        notice = self._transcript.disclosure_text if self._transcript is not None else None
        if notice:
            try:
                await self.session.say(notice, allow_interruptions=False)
                self._transcript.mark_disclosure_played()
            except Exception as exc:  # no notice, no transcript: never record a call that was not told
                LOGGER.error("transcript disclosure could not be played (%s); transcript suppressed", type(exc).__name__)
                self._transcript.suppress()
        self.session.generate_reply(
            instructions="Greet the caller briefly and ask how you can help. Do not claim any external action succeeded unless its Tool reports success."
        )

    @function_tool()
    async def transfer_to_human(self, ctx: RunContext) -> str:
        """Transfer the active SIP caller to the approved human fallback."""
        target = os.environ.get("MODEL_GARDEN_HUMAN_TRANSFER_TARGET", "").strip()
        if not target:
            return "Human transfer is not configured. Offer to capture a message instead."
        if not (target.startswith("tel:") or target.startswith("sip:")):
            return "Human transfer target is invalid. Offer to capture a message instead."
        job_ctx = get_job_context()
        participant = _sip_participant(job_ctx)
        if participant is None:
            return "No active SIP caller is available to transfer. Offer to capture a message instead."
        try:
            await job_ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=job_ctx.room.name,
                    participant_identity=participant.identity,
                    transfer_to=target,
                    play_dialtone=False,
                )
            )
        except Exception as exc:  # provider failure must fall back safely, not end the call
            return f"Human transfer failed ({type(exc).__name__}). Offer to capture a message instead."
        try:
            _append_message(
                {
                    "type": "HumanTransfer",
                    "timestamp": int(time.time()),
                    "roomHash": _safe_digest(job_ctx.room.name),
                    "participantHash": _safe_digest(participant.identity),
                    "targetHash": _safe_digest(target),
                    "status": "requested",
                }
            )
        except Exception as exc:
            # A completed provider transfer cannot be undone by a local storage error.
            LOGGER.error("human transfer receipt not stored: %s", type(exc).__name__)
        if self._transcript is not None:
            self._transcript.note_transfer()
        return "Human transfer requested successfully."

    @function_tool()
    async def capture_message(self, ctx: RunContext, message: str, caller_name: str = "") -> str:
        """Persist the minimum data required for a human to follow up when transfer is unavailable."""
        message = message.strip()
        if not message:
            return "No message was provided. Ask the caller what they would like the business to know."
        job_ctx = get_job_context()
        participant = _sip_participant(job_ctx)
        caller_number = None
        if participant is not None:
            raw = participant.attributes.get("sip.phoneNumber")
            if isinstance(raw, str) and raw.strip():
                caller_number = raw.strip()
        try:
            stored = _append_message(
                {
                    "type": "FallbackMessage",
                    "timestamp": int(time.time()),
                    "roomHash": _safe_digest(job_ctx.room.name),
                    "caller_name": caller_name.strip() or None,
                    "caller_number": caller_number,
                    "message": message,
                }
            )
        except Exception as exc:
            LOGGER.error("fallback message not stored: %s", type(exc).__name__)
            return "The message could not be saved safely. Offer the caller another contact route."
        if not stored:
            return "Message retention is disabled; no message was saved. Offer the caller another contact route."
        if self._transcript is not None:
            self._transcript.note_outcome("message-taken")
        return "Message captured for human follow-up."


def _instructions() -> str:
    path = pathlib.Path("/opt/profile-seed/SOUL.md")
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return (
        "You are the business Receptionist. Use only approved knowledge and Tools. "
        "Minimize caller data, respect approvals, and use transfer_to_human or capture_message when safe completion is not possible."
    )


def build_session() -> AgentSession:
    voice_key = _required("OPENAI_API_KEY")
    hermes_url = _required("MODEL_GARDEN_HERMES_API_URL")
    hermes_key = _required("MODEL_GARDEN_HERMES_API_KEY")
    return AgentSession(
        stt=openai.STT(api_key=voice_key, model="gpt-4o-mini-transcribe"),
        llm=openai.LLM(model="hermes-agent", base_url=hermes_url, api_key=hermes_key),
        tts=openai.TTS(api_key=voice_key, model="gpt-4o-mini-tts", voice="alloy"),
        preemptive_generation=True,
    )


# The pinned LiveKit server's native readiness endpoint stays inside this container.
# Registration and media transport are outbound; no inbound port is published.
server = AgentServer(host="127.0.0.1", port=8081)


@server.rtc_session(agent_name=AGENT_NAME)
async def receptionist(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room_hash": _safe_digest(ctx.room.name), "agent": AGENT_NAME}
    started = int(time.time())
    try:
        _append_message({"type": "CallStarted", "timestamp": started, "roomHash": _safe_digest(ctx.room.name), "agent": AGENT_NAME})
    except Exception as exc:
        LOGGER.error("call start receipt not stored: %s", type(exc).__name__)

    async def on_shutdown() -> None:
        try:
            _append_message(
                {
                    "type": "CallEnded",
                    "timestamp": int(time.time()),
                    "roomHash": _safe_digest(ctx.room.name),
                    "agent": AGENT_NAME,
                    "started": started,
                }
            )
        except Exception as exc:
            LOGGER.error("call end receipt not stored: %s", type(exc).__name__)

    transcript = CallTranscript(_safe_digest(ctx.room.name) or "", AGENT_NAME, TRANSCRIPT_CONFIG, started=started)

    def record_turn(event: Any) -> None:
        # Never let a transcript problem reach the call: log the type only and carry on.
        try:
            item = event.item
            role = getattr(item, "role", None)
            speaker = {"user": "caller", "assistant": "agent"}.get(role)
            text = getattr(item, "text_content", None)
            if speaker and isinstance(text, str):
                confidence = getattr(item, "transcript_confidence", None)  # the speech-to-text confidence, where the model gives one
                transcript.add_turn(
                    speaker, text, interrupted=bool(getattr(item, "interrupted", False)),
                    confidence=confidence if isinstance(confidence, float) and speaker == "caller" else None,
                )
        except Exception as exc:
            LOGGER.error("call transcript turn not recorded: %s", type(exc).__name__)

    async def on_transcript_shutdown() -> None:
        def failed(error_type: str) -> None:
            _append_message(
                {
                    "type": "TranscriptWriteFailed",
                    "timestamp": int(time.time()),
                    "roomHash": _safe_digest(ctx.room.name),
                    "agent": AGENT_NAME,
                    "errorType": error_type,
                }
            )

        finalize_transcript(transcript, on_failure=failed, auto_summary=True)

    ctx.add_shutdown_callback(on_shutdown)
    ctx.add_shutdown_callback(on_transcript_shutdown)
    session = build_session()
    session.on("conversation_item_added", record_turn)
    await session.start(agent=ReceptionistVoiceAgent(_instructions(), transcript), room=ctx.room)
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
