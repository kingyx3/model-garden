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
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any

from livekit import api, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool, get_job_context
from livekit.plugins import openai


MESSAGE_PATH = pathlib.Path(os.environ.get("MODEL_GARDEN_MESSAGE_PATH", "/opt/data/messages.jsonl"))
AGENT_NAME = os.environ.get("MODEL_GARDEN_LIVEKIT_AGENT_NAME", "model-garden-receptionist").strip() or "model-garden-receptionist"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _append_message(payload: dict[str, Any]) -> None:
    MESSAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MESSAGE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")


def _sip_participant(ctx: JobContext):
    for participant in ctx.room.remote_participants.values():
        if participant.kind == rtc.ParticipantKind.SIP:
            return participant
    return None


class ReceptionistVoiceAgent(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)

    async def on_enter(self) -> None:
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
        _append_message(
            {
                "type": "HumanTransfer",
                "timestamp": int(time.time()),
                "room": job_ctx.room.name,
                "participant": participant.identity,
                "target": target,
                "status": "requested",
            }
        )
        return "Human transfer requested successfully."

    @function_tool()
    async def capture_message(self, ctx: RunContext, message: str, caller_name: str = "") -> str:
        """Persist a minimal fallback message when safe transfer is unavailable."""
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
        _append_message(
            {
                "type": "FallbackMessage",
                "timestamp": int(time.time()),
                "room": job_ctx.room.name,
                "caller_name": caller_name.strip() or None,
                "caller_number": caller_number,
                "message": message,
            }
        )
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


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def receptionist(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name, "agent": AGENT_NAME}
    started = int(time.time())
    _append_message({"type": "CallStarted", "timestamp": started, "room": ctx.room.name, "agent": AGENT_NAME})

    async def on_shutdown() -> None:
        _append_message(
            {
                "type": "CallEnded",
                "timestamp": int(time.time()),
                "room": ctx.room.name,
                "agent": AGENT_NAME,
                "started": started,
            }
        )

    ctx.add_shutdown_callback(on_shutdown)
    session = build_session()
    await session.start(agent=ReceptionistVoiceAgent(_instructions()), room=ctx.room)
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
