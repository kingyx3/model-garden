"""Microsoft Teams surface on the shared channel adapter contract (see ``text_channel.py``).

No Entra app registration, bot credential or network connection lives here. This module holds the parts that decide *who is
speaking* and *whether an approval click is genuine*; the deployment supplies a token verifier and a transport that calls
the Bot Framework connector with its own bot token.

- **Request verification fails closed.** Every inbound activity carries a Bot Framework bearer token. Checking the token's
  signature needs Microsoft's published keys, so that step is an injected ``verify_token`` callable returning the token's
  claims; there is no default, and an adapter without one cannot be built. The adapter then checks the claims itself:
  issuer, audience (our bot's app id), expiry and not-before (with a short leeway), and that the token's ``serviceurl``
  claim equals the activity's ``serviceUrl``, which must be https on an allow-listed host. The activity must also come
  from an allow-listed Entra tenant.
- **What the coworker answers.** One-to-one chats, and ``@mentions`` in allow-listed channels or group chats. Bots, the
  coworker's own messages, edits, reactions, typing and membership updates, and ordinary channel chatter are ignored with a
  named reason.
- **Identity.** The sender is the Entra object id Teams reports, scoped to the tenant (``teams: {<tenant>/<objectId>:
  {identity: ...}}``), mapped through the ``PrincipalDirectory``; message text never changes it.
- **Replies go only where we were spoken to.** The service URL for a conversation is taken from a verified inbound activity
  and remembered; a reply to a conversation never seen that way is refused, so nothing can make the coworker post to an
  arbitrary URL. Replies carry ``replyToId`` for the thread.
- **Approvals.** An Adaptive Card ``Action.Submit`` carries the exact ``requestId``. A click is accepted only from a verified
  activity with that payload, the responder is the person who clicked, and ``decide_from_channel`` still requires them to be
  a mapped approver. Deliverables are posted as reference lines (location, digest), not uploaded.

Least-privilege Entra and bot settings are documented in ``docs/channels.md``. Teams redelivers on failure; consumers should
de-duplicate on ``(conversation_id, message_id)``. The remembered service URLs are in memory only: a restart forgets them until
the next verified inbound activity, and proactive messaging is not supported.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import time
import urllib.parse
from collections import OrderedDict
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

CHANNEL = "teams"
ISSUER = "https://api.botframework.com"
DEFAULT_SERVICE_HOSTS = ("smba.trafficmanager.net",)
LEEWAY_SECONDS = 300
MAX_BODY_BYTES = 1024 * 1024
MAX_TOKEN_CHARS = 8192
MAX_TEXT_CHARS = 20000
MAX_REMEMBERED = 1000
FILE_INFO = "application/vnd.microsoft.teams.file.download.info"
ADAPTIVE_CARD = "application/vnd.microsoft.card.adaptive"
ACTION_KEY = "mgAction"
DECISION_ACTIONS = {"approve": "approve", "reject": "reject"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
_CONVERSATION = re.compile(r"^[A-Za-z0-9:;=@._-]{1,256}$")
_ACTIVITY_ID = re.compile(r"^[A-Za-z0-9:;=@._|-]{1,256}$")
_AT = re.compile(r"<at>.*?</at>", re.IGNORECASE | re.DOTALL)
_BOT_ID = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")


class TeamsIgnored(Exception):
    """A verified activity that the coworker deliberately does not act on; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return str(value)
    return ""


def _host_allowed(url: str, allowed: tuple[str, ...]) -> bool:
    parts = urllib.parse.urlparse(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and not parts.username and not parts.password and parts.port in (None, 443) and any(
        host == item or host.endswith("." + item) for item in allowed
    )


class TeamsChannelAdapter:
    """``TextChannelAdapter`` for Teams. The bot credential and HTTP call belong to the injected transport."""

    channel = CHANNEL

    def __init__(
        self,
        *,
        bot_app_id: str,
        allowed_tenants: tuple[str, ...],
        verify_token: Callable[[str], Mapping[str, Any]],
        allowed_conversations: tuple[str, ...] = (),
        allowed_service_hosts: tuple[str, ...] = DEFAULT_SERVICE_HOSTS,
        issuers: tuple[str, ...] = (ISSUER,),
        transport: Callable[[dict[str, Any]], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(verify_token):
            raise ValueError("a Teams adapter needs a token verifier; it will not accept unauthenticated activities")
        if not isinstance(bot_app_id, str) or not _GUID.match(bot_app_id):
            raise ValueError("bot app id must be the bot's Entra application (client) id")
        if not allowed_tenants or not all(isinstance(item, str) and _GUID.match(item) for item in allowed_tenants):
            raise ValueError("allowed tenants must list at least one Entra tenant id")
        if not all(isinstance(item, str) and _CONVERSATION.match(item) and ";" not in item for item in allowed_conversations):
            raise ValueError("allowed conversations must be Teams conversation ids without a thread suffix")
        if not allowed_service_hosts or not all(isinstance(item, str) and re.fullmatch(r"[a-z0-9.-]+", item) for item in allowed_service_hosts):
            raise ValueError("allowed service hosts must be bare host names")
        self._verify_token = verify_token
        self._app_id = bot_app_id.lower()
        self._tenants = frozenset(item.lower() for item in allowed_tenants)
        self._conversations = frozenset(allowed_conversations)
        self._hosts = tuple(allowed_service_hosts)
        self._issuers = tuple(issuers)
        self._transport = transport
        self._clock = clock
        self._service_urls: OrderedDict[str, str] = OrderedDict()

    # -- inbound ------------------------------------------------------------------------

    def _verified(self, body: bytes, headers: Mapping[str, str]) -> dict[str, Any]:
        """The activity, once the bearer token, tenant and service URL all check out."""
        if not isinstance(body, (bytes, bytearray)) or not body or len(body) > MAX_BODY_BYTES:
            raise ChannelVerificationError("Teams request body is empty or too large")
        authorization = _header(headers, "Authorization")
        if not authorization.startswith("Bearer ") or not 8 < len(authorization) <= MAX_TOKEN_CHARS:
            raise ChannelVerificationError("Teams request is missing its bearer token")
        try:
            claims = self._verify_token(authorization[7:])
        except ChannelVerificationError:
            raise
        except Exception as exc:  # the verifier's own failure is a failed verification, never a pass
            raise ChannelVerificationError("Teams bearer token could not be verified") from exc
        if not isinstance(claims, Mapping):
            raise ChannelVerificationError("Teams token verifier returned no claims")
        now = self._clock()
        audience = claims.get("aud")
        if claims.get("iss") not in self._issuers or not isinstance(audience, str) or audience.lower() != self._app_id:
            raise ChannelVerificationError("Teams token has the wrong issuer or audience")
        expires, not_before = claims.get("exp"), claims.get("nbf")
        if not isinstance(expires, (int, float)) or expires + LEEWAY_SECONDS < now:
            raise ChannelVerificationError("Teams token has expired")
        if not_before is not None and (not isinstance(not_before, (int, float)) or not_before - LEEWAY_SECONDS > now):
            raise ChannelVerificationError("Teams token is not valid yet")
        try:
            activity = json.loads(bytes(body).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelVerificationError("Teams request body is not valid JSON") from exc
        if not isinstance(activity, dict):
            raise ChannelVerificationError("Teams request body is not a JSON object")
        service_url = activity.get("serviceUrl")
        if not isinstance(service_url, str) or not _host_allowed(service_url, self._hosts):
            raise ChannelVerificationError("Teams service URL is not an allowed https host")
        claimed = claims.get("serviceurl")
        if claimed is not None and (not isinstance(claimed, str) or claimed.rstrip("/") != service_url.rstrip("/")):
            raise ChannelVerificationError("Teams service URL does not match the token")
        tenant = ((activity.get("channelData") or {}).get("tenant") or {}).get("id") or (activity.get("conversation") or {}).get("tenantId")
        if not isinstance(tenant, str) or tenant.lower() not in self._tenants:
            raise ChannelVerificationError("Teams activity is not from an allowed tenant")
        return activity

    @staticmethod
    def _tenant(activity: Mapping[str, Any]) -> str:
        return str(((activity.get("channelData") or {}).get("tenant") or {}).get("id") or (activity.get("conversation") or {}).get("tenantId")).lower()

    def _remember(self, conversation_id: str, service_url: str) -> None:
        self._service_urls[conversation_id] = service_url.rstrip("/")
        self._service_urls.move_to_end(conversation_id)
        while len(self._service_urls) > MAX_REMEMBERED:
            self._service_urls.popitem(last=False)

    def parse_inbound(self, body: bytes, headers: Mapping[str, str]) -> InboundMessage:
        """A verified one-to-one message or ``@mention`` as an ``InboundMessage``; ``TeamsIgnored`` for the rest."""
        activity = self._verified(body, headers)
        if activity.get("type") != "message":
            raise TeamsIgnored("unsupported-activity")
        value = activity.get("value")
        if isinstance(value, dict) and ACTION_KEY in value:
            raise TeamsIgnored("approval-submit")
        sender, recipient, conversation = activity.get("from"), activity.get("recipient"), activity.get("conversation")
        if not (isinstance(sender, dict) and isinstance(recipient, dict) and isinstance(conversation, dict)):
            raise TeamsIgnored("malformed-activity")
        if sender.get("role") == "bot":
            raise TeamsIgnored("not-a-person")
        bot_id = recipient.get("id")
        if not isinstance(bot_id, str) or not _BOT_ID.match(bot_id):
            raise TeamsIgnored("malformed-activity")
        if sender.get("id") == bot_id:
            raise TeamsIgnored("own-message")
        user = sender.get("aadObjectId")
        if not isinstance(user, str) or not _GUID.match(user):
            raise TeamsIgnored("no-aad-identity")
        conversation_id, message_id = conversation.get("id"), activity.get("id")
        if not (isinstance(conversation_id, str) and _CONVERSATION.match(conversation_id) and isinstance(message_id, str) and _ACTIVITY_ID.match(message_id)):
            raise TeamsIgnored("malformed-activity")
        kind = conversation.get("conversationType")
        if kind not in ("personal", "channel", "groupChat"):
            raise TeamsIgnored("unsupported-conversation")
        if kind != "personal":
            mentioned = any(
                isinstance(entity, dict) and entity.get("type") == "mention"
                and isinstance(entity.get("mentioned"), dict) and entity["mentioned"].get("id") == bot_id
                for entity in (activity.get("entities") or [])
            )
            if not mentioned:
                raise TeamsIgnored("not-addressed-to-the-coworker")
            if conversation_id.split(";", 1)[0] not in self._conversations:
                raise TeamsIgnored("conversation-not-allowed")
        reply_to = activity.get("replyToId")
        thread = (reply_to if isinstance(reply_to, str) and _ACTIVITY_ID.match(reply_to) else message_id) if kind != "personal" else None
        text = _AT.sub("", str(activity.get("text") or "")).strip()[:MAX_TEXT_CHARS]
        attachments = tuple(
            Attachment(
                attachment_id=str((item.get("content") or {}).get("uniqueId") or item.get("name") or "file")[:64],
                name=str(item.get("name") or "file")[:256], media_type="application/octet-stream",
                size_bytes=0,
            )
            for item in (activity.get("attachments") or []) if isinstance(item, dict) and item.get("contentType") == FILE_INFO
        )
        self._remember(conversation_id, str(activity["serviceUrl"]))
        return InboundMessage(
            channel=CHANNEL, conversation_id=conversation_id, thread_id=thread, message_id=message_id,
            sender=ChannelIdentity(channel=CHANNEL, channel_user_id=user.lower(), workspace_id=self._tenant(activity)),
            text=text, attachments=attachments,
        )

    def parse_approval_response(self, body: bytes, headers: Mapping[str, str]) -> ApprovalResponse | None:
        """A decision only from a verified activity carrying one of our Adaptive Card ``Action.Submit`` payloads."""
        activity = self._verified(body, headers)
        value = activity.get("value")
        if activity.get("type") != "message" or not isinstance(value, dict) or ACTION_KEY not in value:
            return None
        decision = DECISION_ACTIONS.get(value.get(ACTION_KEY))
        request_id = value.get("requestId")
        sender = activity.get("from")
        user = sender.get("aadObjectId") if isinstance(sender, dict) else None
        if decision is None or not (isinstance(request_id, str) and _REQUEST_ID.match(request_id) and isinstance(user, str) and _GUID.match(user)):
            raise ChannelVerificationError("Teams approval click is missing its request, decision or user")
        if sender.get("role") == "bot":
            raise ChannelVerificationError("Teams approval click did not come from a person")
        return ApprovalResponse(
            request_id=request_id, decision=decision,
            responder=ChannelIdentity(channel=CHANNEL, channel_user_id=user.lower(), workspace_id=self._tenant(activity)),
        )

    # -- outbound: composed here, posted by the injected transport -------------------------

    def _post(self, conversation_id: str, thread_id: str | None, text: str, attachments: list[dict[str, Any]] | None = None) -> str:
        if self._transport is None:
            raise RuntimeError("no Teams transport is configured for this deployment")
        service_url = self._service_urls.get(conversation_id)
        if service_url is None:
            raise RuntimeError("unknown conversation: Teams replies go only to conversations seen in a verified inbound activity")
        activity: dict[str, Any] = {"type": "message", "text": text}
        if thread_id:
            activity["replyToId"] = thread_id
        if attachments:
            activity["attachments"] = attachments
        return self._transport({"serviceUrl": service_url, "conversationId": conversation_id, "activity": activity})

    def send_reply(self, reply: OutboundReply) -> str:
        """Post the reply in its thread; a deliverable is posted as a reference line (location, digest), not uploaded."""
        text = "\n\n".join([reply.text[:MAX_TEXT_CHARS], *TEXT.describe_attachments(reply.attachments)])
        return self._post(reply.conversation_id, reply.thread_id, text[:MAX_TEXT_CHARS])

    def send_receipt(self, receipt: ActionReceipt) -> str:
        return self._post(
            receipt.conversation_id, receipt.thread_id,
            f"{receipt.summary} ({receipt.tool}: {receipt.status}; ledger reference {receipt.request_id})",
        )

    def send_approval_prompt(self, prompt: ApprovalPrompt) -> str:
        text = f"Approval needed for {prompt.tool}: {prompt.summary}"[:2900]
        card = {
            "type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
            "body": [{"type": "TextBlock", "text": text, "wrap": True}],
            "actions": [
                {"type": "Action.Submit", "title": "Approve", "style": "positive", "data": {ACTION_KEY: "approve", "requestId": prompt.request_id}},
                {"type": "Action.Submit", "title": "Reject", "style": "destructive", "data": {ACTION_KEY: "reject", "requestId": prompt.request_id}},
            ],
        }
        return self._post(prompt.conversation_id, prompt.thread_id, text, [{"contentType": ADAPTIVE_CARD, "content": card}])
