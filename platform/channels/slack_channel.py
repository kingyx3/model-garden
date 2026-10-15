"""Slack surface on the shared channel adapter contract (see ``text_channel.py``).

No Slack app, token or network connection lives here. This module holds the parts that decide *who is
speaking* and *whether a button click is genuine*; the deployment supplies the signing secret and a
transport that calls ``chat.postMessage`` with its own bot token.

- **Request verification.** Every inbound request, events and interactive payloads alike, must carry a
  valid ``X-Slack-Signature`` (``v0=`` HMAC-SHA256 of ``v0:<timestamp>:<raw body>`` with the app's
  signing secret) and a timestamp within five minutes, so a captured request cannot be replayed later.
  The raw body is verified before anything in it is read.
- **What the coworker answers.** Direct messages and ``@mentions`` only. Channel traffic is answered
  only for channels in the deployment's allow-list; everything else, the bot's own and other bots'
  messages, edits and deletions, and ordinary channel chatter, is ignored with a named reason.
- **Identity.** The sender is the Slack user and workspace the event reports, mapped through the
  ``PrincipalDirectory`` (``slack: {T01/U02: {identity: ...}}``); message text never changes it.
- **Threads.** Conversations are keyed by channel and thread (``thread_ts`` or the message ``ts``),
  and every outgoing message is posted into that thread.
- **Approvals.** Buttons carry the exact ``requestId`` as their value. A click is accepted only from a
  verified interactive payload with one of our two action IDs; the responder is the Slack user who
  clicked, and ``decide_from_channel`` still requires them to be a mapped approver.

Least-privilege scopes for the app are documented in ``docs/channels.md``. Retries: Slack redelivers
an event it thinks failed; consumers should de-duplicate on ``(conversation_id, message_id)``.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import pathlib
import re
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any


def _sibling(name: str, filename: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


TEXT = _sibling("model_garden_text_channel", "text_channel.py")
ActionReceipt = TEXT.ActionReceipt
ApprovalPrompt = TEXT.ApprovalPrompt
ApprovalResponse = TEXT.ApprovalResponse
Attachment = TEXT.Attachment
ChannelIdentity = TEXT.ChannelIdentity
ChannelVerificationError = TEXT.ChannelVerificationError
InboundMessage = TEXT.InboundMessage
OutboundReply = TEXT.OutboundReply

CHANNEL = "slack"
TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 20000
APPROVE_ACTION = "mg_approve"
REJECT_ACTION = "mg_reject"
# Subtypes that are still a person speaking; everything else (bot_message, message_changed, ...) is noise.
HUMAN_SUBTYPES = frozenset({None, "file_share", "thread_broadcast"})
_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Z0-9]{2,32}$")
_TS = re.compile(r"^\d{9,12}\.\d{1,8}$")
_MENTION = re.compile(r"<@[A-Z0-9]{2,32}(?:\|[^>]*)?>")


class SlackIgnored(Exception):
    """A verified request that the coworker deliberately does not act on; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return str(value)
    return ""


def verify_request(
    body: bytes,
    headers: Mapping[str, str],
    signing_secret: bytes,
    *,
    now: float | None = None,
    tolerance_seconds: int = TOLERANCE_SECONDS,
) -> None:
    """Raise ``ChannelVerificationError`` unless ``body`` is a fresh, correctly signed Slack request."""
    if not isinstance(body, (bytes, bytearray)) or not body or len(body) > MAX_BODY_BYTES:
        raise ChannelVerificationError("Slack request body is empty or too large")
    timestamp, signature = _header(headers, "X-Slack-Request-Timestamp"), _header(headers, "X-Slack-Signature")
    if not timestamp.isdigit() or not signature.startswith("v0="):
        raise ChannelVerificationError("Slack request is missing its timestamp or signature")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > tolerance_seconds:
        raise ChannelVerificationError("Slack request timestamp is outside the replay window")
    expected = "v0=" + hmac.new(
        signing_secret, b"v0:" + timestamp.encode("ascii") + b":" + bytes(body), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ChannelVerificationError("Slack request signature is not valid")


def sign_request(body: bytes, signing_secret: bytes, timestamp: int) -> dict[str, str]:
    """Headers Slack would send for ``body``; for tests and local replay tooling."""
    signature = "v0=" + hmac.new(signing_secret, f"v0:{timestamp}:".encode("ascii") + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(timestamp), "X-Slack-Signature": signature}


class SlackChannelAdapter:
    """``TextChannelAdapter`` for Slack. The bot token and HTTP call belong to the injected transport."""

    channel = CHANNEL

    def __init__(
        self,
        signing_secret: bytes,
        *,
        bot_user_id: str,
        allowed_channels: tuple[str, ...] = (),
        transport: Callable[[dict[str, Any]], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(signing_secret, (bytes, bytearray)) or len(signing_secret) < 16:
            raise ValueError("Slack signing secret must be at least 16 bytes")
        if not isinstance(bot_user_id, str) or not _ID.match(bot_user_id):
            raise ValueError("bot user id must look like a Slack user ID such as U0123ABC")
        bad = [item for item in allowed_channels if not isinstance(item, str) or not _ID.match(item)]
        if bad:
            raise ValueError("allowed channels must be Slack channel IDs such as C0123ABC")
        self._secret = bytes(signing_secret)
        self._bot = bot_user_id
        self._allowed = frozenset(allowed_channels)
        self._transport = transport
        self._clock = clock

    # -- inbound ------------------------------------------------------------------------

    def _verified(self, body: bytes, headers: Mapping[str, str]) -> None:
        verify_request(body, headers, self._secret, now=self._clock())

    def handle_challenge(self, body: bytes, headers: Mapping[str, str]) -> str | None:
        """The challenge to echo for Slack's one-time ``url_verification`` request, else ``None``."""
        self._verified(body, headers)
        document = self._json(body)
        if document.get("type") == "url_verification" and isinstance(document.get("challenge"), str):
            return document["challenge"][:256]
        return None

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        try:
            document = json.loads(bytes(body).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelVerificationError("Slack request body is not valid JSON") from exc
        if not isinstance(document, dict):
            raise ChannelVerificationError("Slack request body is not a JSON object")
        return document

    def parse_inbound(self, body: bytes, headers: Mapping[str, str]) -> InboundMessage:
        """A verified direct message or ``@mention`` as an ``InboundMessage``; ``SlackIgnored`` for the rest."""
        self._verified(body, headers)
        document = self._json(body)
        if document.get("type") != "event_callback":
            raise SlackIgnored("not-an-event")
        event = document.get("event")
        if not isinstance(event, dict):
            raise SlackIgnored("no-event")
        kind = event.get("type")
        if kind not in ("message", "app_mention"):
            raise SlackIgnored("unsupported-event")
        if event.get("bot_id") or event.get("subtype") not in HUMAN_SUBTYPES:
            raise SlackIgnored("not-a-person")
        user, channel, stamp = event.get("user"), event.get("channel"), event.get("ts")
        if not (isinstance(user, str) and _ID.match(user) and isinstance(channel, str) and _ID.match(channel)
                and isinstance(stamp, str) and _TS.match(stamp)):
            raise SlackIgnored("malformed-event")
        if user == self._bot:
            raise SlackIgnored("own-message")
        direct = kind == "message" and event.get("channel_type") == "im"
        mention = kind == "app_mention"
        if not (direct or mention):
            raise SlackIgnored("not-addressed-to-the-coworker")
        if mention and channel not in self._allowed:
            raise SlackIgnored("channel-not-allowed")
        team = document.get("team_id") or event.get("team")
        if not isinstance(team, str) or not _ID.match(team):
            raise SlackIgnored("no-workspace")
        thread = event.get("thread_ts") if isinstance(event.get("thread_ts"), str) and _TS.match(event["thread_ts"]) else stamp
        text = _MENTION.sub("", str(event.get("text") or "")).strip()[:MAX_TEXT_CHARS]
        attachments = tuple(
            Attachment(attachment_id=str(item["id"])[:64], name=str(item.get("name") or "file")[:256],
                       media_type=str(item.get("mimetype") or "application/octet-stream")[:128],
                       size_bytes=int(item["size"]) if isinstance(item.get("size"), int) else 0)
            for item in (event.get("files") or []) if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
        return InboundMessage(
            channel=CHANNEL, conversation_id=channel, thread_id=thread, message_id=stamp,
            sender=ChannelIdentity(channel=CHANNEL, channel_user_id=user, workspace_id=team),
            text=text, attachments=attachments,
        )

    def parse_approval_response(self, body: bytes, headers: Mapping[str, str]) -> ApprovalResponse | None:
        """A decision only from a verified ``block_actions`` payload clicking one of our two buttons."""
        self._verified(body, headers)
        fields = urllib.parse.parse_qs(bytes(body).decode("utf-8", "replace"), keep_blank_values=True)
        raw = (fields.get("payload") or [""])[0]
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ChannelVerificationError("Slack interactive payload is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("type") != "block_actions":
            return None
        actions = payload.get("actions")
        action = actions[0] if isinstance(actions, list) and len(actions) == 1 and isinstance(actions[0], dict) else None
        if action is None or action.get("action_id") not in (APPROVE_ACTION, REJECT_ACTION):
            return None
        value, user, team = action.get("value"), (payload.get("user") or {}), (payload.get("team") or {})
        user_id, team_id = user.get("id"), team.get("id")
        if not (isinstance(value, str) and _REQUEST_ID.match(value) and isinstance(user_id, str) and _ID.match(user_id)
                and isinstance(team_id, str) and _ID.match(team_id)):
            raise ChannelVerificationError("Slack approval click is missing its request, user or workspace")
        return ApprovalResponse(
            request_id=value,
            decision="approve" if action["action_id"] == APPROVE_ACTION else "reject",
            responder=ChannelIdentity(channel=CHANNEL, channel_user_id=user_id, workspace_id=team_id),
        )

    # -- outbound: composed here, posted by the injected transport -------------------------

    def _post(self, conversation_id: str, thread_id: str | None, text: str, blocks: list[dict[str, Any]] | None = None) -> str:
        if self._transport is None:
            raise RuntimeError("no Slack transport is configured for this deployment")
        message: dict[str, Any] = {"method": "chat.postMessage", "channel": conversation_id, "text": text}
        if thread_id:
            message["thread_ts"] = thread_id  # every reply stays in the thread
        if blocks:
            message["blocks"] = blocks
        return self._transport(message)

    def send_reply(self, reply: OutboundReply) -> str:
        return self._post(reply.conversation_id, reply.thread_id, reply.text[:MAX_TEXT_CHARS])

    def send_receipt(self, receipt: ActionReceipt) -> str:
        return self._post(
            receipt.conversation_id, receipt.thread_id,
            f"{receipt.summary} ({receipt.tool}: {receipt.status}; ledger reference {receipt.request_id})",
        )

    def send_approval_prompt(self, prompt: ApprovalPrompt) -> str:
        text = f"Approval needed for {prompt.tool}: {prompt.summary}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": APPROVE_ACTION, "style": "primary", "value": prompt.request_id,
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": REJECT_ACTION, "style": "danger", "value": prompt.request_id,
                 "text": {"type": "plain_text", "text": "Reject"}},
            ]},
        ]
        return self._post(prompt.conversation_id, prompt.thread_id, text, blocks)
