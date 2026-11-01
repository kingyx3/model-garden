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

import asyncio
import hashlib
import logging
import os
import pathlib
import re
import secrets
import time
from typing import Any

from livekit import api, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool, get_job_context
from livekit.plugins import openai

from call_transcript import CallTranscript, config_from_env as transcript_config_from_env, finalize as finalize_transcript, redact, store_private_jsonl
from voice_pause import HOLDING, paused as voice_paused, stop_session_on_pause

LOGGER = logging.getLogger("model-garden.receptionist")
# An unsafe transcript configuration (for example no disclosure and no lawful-basis owner) stops the worker at start.
TRANSCRIPT_CONFIG = transcript_config_from_env()

MESSAGE_PATH = pathlib.Path(os.environ.get("MODEL_GARDEN_MESSAGE_PATH", "/opt/data/messages.jsonl"))
AGENT_NAME = os.environ.get("MODEL_GARDEN_LIVEKIT_AGENT_NAME", "model-garden-receptionist").strip() or "model-garden-receptionist"
DEFAULT_MESSAGE_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_FALLBACK_MESSAGE_CHARS = 4000
MAX_FALLBACK_NAME_CHARS = 200


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


def _call_hash(room_name: str) -> str:
    """Per-invocation opaque identity; the separate room digest is not a call ID."""
    return hashlib.sha256(secrets.token_bytes(32) + room_name.encode("utf-8")).hexdigest()


def _append_message(payload: dict[str, Any]) -> bool:
    """Store private fallback/lifecycle records; report whether retention allowed saving."""
    retention = _retention_seconds()
    store_private_jsonl(MESSAGE_PATH, payload, retention_seconds=retention)
    return retention > 0


def _sip_participant(ctx: JobContext):
    """Select only a unique SIP caller; never guess in a multi-party room.

    A room may temporarily contain multiple SIP participants during a
    conference or reconnect. Selecting the first would risk transferring the
    wrong caller or attaching another caller's number to a saved message.
    """
    selected = None
    for participant in ctx.room.remote_participants.values():
        if participant.kind == rtc.ParticipantKind.SIP:
            if selected is not None:
                raise ValueError("ambiguous SIP participants")
            selected = participant
    return selected


class ReceptionistVoiceAgent(Agent):
    def __init__(self, instructions: str, transcript: CallTranscript | None = None, *, call_hash: str | None = None,
                 project: str | None = None) -> None:
        super().__init__(instructions=instructions)
        self._transcript = transcript
        self._call_hash = call_hash
        self._project = project

    async def on_enter(self) -> None:
        # The owner pause must stop new model dialogue, not just governed Tools.
        # A holding response is the only permitted speech while paused.
        if await voice_paused():
            if self._transcript is not None:
                self._transcript.suppress()
            await self.session.say(HOLDING, allow_interruptions=False)
            return
        notice = self._transcript.disclosure_text if self._transcript is not None else None
        if notice:
            try:
                await self.session.say(notice, allow_interruptions=False)
                self._transcript.mark_disclosure_played()
            except Exception as exc:  # no notice, no transcript: never record a call that was not told
                LOGGER.error("transcript disclosure could not be played (%s); transcript suppressed", type(exc).__name__)
                self._transcript.suppress()
        # Disclosure playback may have taken time; recheck before starting the model.
        if await voice_paused():
            if self._transcript is not None:
                self._transcript.suppress()
            await self.session.say(HOLDING, allow_interruptions=False)
            return
        self.session.generate_reply(
            instructions="Greet the caller briefly and ask how you can help. Do not claim any external action succeeded unless its Tool reports success."
        )

    @function_tool()
    async def transfer_to_human(self, ctx: RunContext) -> str:
        """Transfer the active SIP caller to the approved human fallback."""
        if await voice_paused():
            return HOLDING
        target = os.environ.get("MODEL_GARDEN_HUMAN_TRANSFER_TARGET", "").strip()
        if not target:
            return "Human transfer is not configured. Offer to capture a message instead."
        if not (target.startswith("tel:") or target.startswith("sip:")):
            return "Human transfer target is invalid. Offer to capture a message instead."
        job_ctx = get_job_context()
        try:
            participant = _sip_participant(job_ctx)
        except ValueError:
            return "Multiple SIP callers are present; cannot safely transfer one. Offer a separate contact route."
        if participant is None:
            return "No active SIP caller is available to transfer. Offer to capture a message instead."
        # Recheck immediately before provider transport. This is not a
        # cross-process in-flight cancellation guarantee.
        if await voice_paused():
            return HOLDING
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
                    "agent": AGENT_NAME,
                    "roomHash": _safe_digest(job_ctx.room.name),
                    "callHash": self._call_hash,
                    "project": self._project,
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
        if await voice_paused():
            return HOLDING
        if not isinstance(message, str) or not isinstance(caller_name, str):
            return "Invalid message or caller name. Ask the caller to provide a short text message."
        message = message.strip()
        caller_name = caller_name.strip()
        if not message:
            return "No message was provided. Ask the caller what they would like the business to know."
        # Bound untrusted Tool arguments before running client-configured regexes.
        # Fallback storage has its own retention policy: transcript recording
        # being disabled must never disable payment-card/client-pattern redaction.
        if len(message) > MAX_FALLBACK_MESSAGE_CHARS or len(caller_name) > MAX_FALLBACK_NAME_CHARS:
            return "The message or caller name is too long to save safely. Ask the caller to shorten it."
        try:
            safe_message, message_redactions = redact(message, TRANSCRIPT_CONFIG.patterns)
            safe_name, name_redactions = redact(caller_name, TRANSCRIPT_CONFIG.patterns)
        except Exception as exc:
            LOGGER.error("fallback message redaction failed: %s", type(exc).__name__)
            return "The message could not be saved safely. Offer the caller another contact route."
        redactions = {key: message_redactions[key] + name_redactions[key] for key in ("card", "pattern")}
        job_ctx = get_job_context()
        try:
            participant = _sip_participant(job_ctx)
        except ValueError:
            return "Multiple SIP callers are present; cannot safely attribute a message. Offer a separate contact route."
        caller_number = None
        if participant is not None:
            raw = participant.attributes.get("sip.phoneNumber")
            if isinstance(raw, str) and raw.strip():
                caller_number = raw.strip()
        if await voice_paused():
            return HOLDING
        try:
            stored = _append_message(
                {
                    "type": "FallbackMessage",
                    "timestamp": int(time.time()),
                    "agent": AGENT_NAME,
                    "roomHash": _safe_digest(job_ctx.room.name),
                    "callHash": self._call_hash,
                    "project": self._project,
                    "caller_name": safe_name or None,
                    "caller_number": caller_number,
                    "message": safe_message,
                    "redactions": redactions,
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


def _register_sip_disconnect_flush(ctx: JobContext, session: AgentSession) -> None:
    """Best-effort partial-turn flush when the last SIP caller hangs up.

    A room may stay connected because of observers even after its SIP caller
    leaves. Closing the AgentSession gives LiveKit an opportunity to commit
    interrupted TTS text before the job's transcript shutdown callback.
    This is not proof of what the caller actually heard; a process crash or
    provider failure can still lose a partial turn.
    """
    closing = False

    @ctx.room.on("participant_disconnected")
    def _on_sip_disconnect(participant: rtc.RemoteParticipant) -> None:
        nonlocal closing
        if closing or getattr(participant, "kind", None) != rtc.ParticipantKind.SIP:
            return
        identity = getattr(participant, "identity", None)
        if not isinstance(identity, str) or not identity:
            LOGGER.error("SIP disconnect identity unavailable; transcript completeness unverified")
            return
        try:
            # Ignore the departing caller even if the SDK has not yet removed
            # it from the room map. Never terminate a remaining SIP caller.
            if any(
                getattr(other, "kind", None) == rtc.ParticipantKind.SIP
                and getattr(other, "identity", None) != identity
                for other in ctx.room.remote_participants.values()
            ):
                return
        except (AttributeError, TypeError):
            LOGGER.error("SIP disconnect room state unavailable; transcript completeness unverified")
            return
        closing = True

        async def _flush() -> None:
            try:
                await session.aclose()
            except Exception as exc:
                # Do not log provider exceptions: they may contain caller data.
                LOGGER.error("voice session close failed (%s); transcript completeness unverified",
                             type(exc).__name__)

        asyncio.create_task(_flush())


def build_session(*, call_hash: str) -> AgentSession:
    # The pinned Hermes API derives a headerless session from the first user
    # message and system prompt. Two callers saying "hello" must never share
    # a Hermes conversation or its model/Tool context. The random per-invocation
    # call hash is generated by this worker, not supplied by a SIP participant.
    if not isinstance(call_hash, str) or re.fullmatch(r"[0-9a-f]{64}", call_hash) is None:
        raise ValueError("invalid voice call session identity")
    voice_key = _required("OPENAI_API_KEY")
    hermes_url = _required("MODEL_GARDEN_HERMES_API_URL")
    hermes_key = _required("MODEL_GARDEN_HERMES_API_KEY")
    return AgentSession(
        stt=openai.STT(api_key=voice_key, model="gpt-4o-mini-transcribe"),
        llm=openai.LLM(
            model="hermes-agent", base_url=hermes_url, api_key=hermes_key,
            extra_headers={"X-Hermes-Session-Id": f"voice-{call_hash}"},
        ),
        tts=openai.TTS(api_key=voice_key, model="gpt-4o-mini-tts", voice="alloy"),
        preemptive_generation=True,
    )


# The pinned LiveKit server's native readiness endpoint stays inside this container.
# Registration and media transport are outbound; no inbound port is published.
server = AgentServer(host="127.0.0.1", port=8081)


@server.rtc_session(agent_name=AGENT_NAME)
async def receptionist(ctx: JobContext) -> None:
    # A voice volume must not be reused as another client deployment's evidence.
    project = _required("MODEL_GARDEN_VOICE_PROJECT")
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", project) is None:
        raise RuntimeError("MODEL_GARDEN_VOICE_PROJECT must be a valid Compose project")
    room_hash = _safe_digest(ctx.room.name)
    call_hash = _call_hash(ctx.room.name)
    ctx.log_context_fields = {"room_hash": room_hash, "call_hash": call_hash, "agent": AGENT_NAME}
    started = int(time.time())
    try:
        _append_message({"type": "CallStarted", "timestamp": started, "roomHash": room_hash, "callHash": call_hash, "agent": AGENT_NAME, "project": project})
    except Exception as exc:
        LOGGER.error("call start receipt not stored: %s", type(exc).__name__)

    async def on_shutdown() -> None:
        try:
            _append_message(
                {
                    "type": "CallEnded",
                    "timestamp": int(time.time()),
                    "roomHash": room_hash,
                    "callHash": call_hash,
                    "agent": AGENT_NAME,
                    "project": project,
                    "started": started,
                }
            )
        except Exception as exc:
            LOGGER.error("call end receipt not stored: %s", type(exc).__name__)

    transcript = CallTranscript(call_hash, AGENT_NAME, TRANSCRIPT_CONFIG, started=started, project=project)

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
                    "roomHash": room_hash,
                    "callHash": call_hash,
                    "agent": AGENT_NAME,
                    "project": project,
                    "errorType": error_type,
                }
            )

        finalize_transcript(transcript, on_failure=failed, auto_summary=True)

    # Cancel the watcher before normal transcript finalization on job shutdown.
    # The watcher may itself trigger shutdown while closing a paused session;
    # never cancel or await the current task in that case.
    monitor_task: asyncio.Task[None] | None = None

    async def stop_pause_monitor() -> None:
        if monitor_task is None or monitor_task is asyncio.current_task():
            return
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    ctx.add_shutdown_callback(stop_pause_monitor)
    ctx.add_shutdown_callback(on_shutdown)
    ctx.add_shutdown_callback(on_transcript_shutdown)
    session = build_session(call_hash=call_hash)
    _register_sip_disconnect_flush(ctx, session)
    session.on("conversation_item_added", record_turn)
    await session.start(agent=ReceptionistVoiceAgent(_instructions(), transcript, call_hash=call_hash, project=project), room=ctx.room)
    await ctx.connect()
    # Admission-only checks do not stop an already active conversation after
    # owner pause. Poll the private sidecar and close this session on pause or
    # loss of the trusted status route. No new model or provider credentials.
    monitor_task = asyncio.create_task(stop_session_on_pause(session))


if __name__ == "__main__":
    cli.run_app(server)
