"""Built-in web chat authentication and adapter on the shared channel contract.

The hosted page, HTTP server and sign-in email are in the channel gateway (``platform/runtime/gateway.py``). This module is the part that decides *who is typing*:

- **Magic-link sign-in, no passwords.** A sign-in link can be issued only for an address on the client's
  allow-list (``issue_login`` returns ``None`` for anyone else, so the page can say the same thing in both
  cases and never reveals who is on the list). The link is signed, expires in 15 minutes by default and
  can be used once; using it returns a session token.
- **Short-lived, scoped sessions.** A session token is signed under a different label from a link, so a
  link cannot be replayed as a session, expires (8 hours by default, at most 24) and names the
  client/environment ``scope`` it was issued for; a token from another client or environment is refused.
- **Authority from the session, never the request.** ``parse_inbound`` and ``parse_approval_response``
  read the sender only from the ``Authorization: Bearer`` session. A user id, name or approver claim in the
  JSON body is ignored. The session identity is then mapped through the ``PrincipalDirectory``
  (``webchat: {alice@client.example: {identity: ..., roles: [approver]}}``), so a signed-in but unmapped
  address is still a public, read-only principal.
- **No public admin surface.** There is no endpoint, account or token that can change policy or approve on
  anyone's behalf here; approvals go through ``decide_from_channel`` like every other surface.

The signing key (at least 32 bytes) and the spent-link directory are supplied by the deployment; spent
markers are small files kept for the life of the environment.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib.util
import json
import os
import pathlib
import re
import secrets
import sys
import time
from collections.abc import Callable, Iterable, Mapping
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
ChannelIdentity = TEXT.ChannelIdentity
ChannelVerificationError = TEXT.ChannelVerificationError
InboundMessage = TEXT.InboundMessage
OutboundReply = TEXT.OutboundReply

CHANNEL = "webchat"
MIN_SECRET_BYTES = 32
DEFAULT_LOGIN_TTL_SECONDS = 15 * 60
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_LOGIN_TTL_SECONDS = 60 * 60
MAX_SESSION_TTL_SECONDS = 24 * 60 * 60
MAX_BODY_BYTES = 256 * 1024
MAX_TEXT_CHARS = 20000
_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:~-]{1,128}$")
_SCOPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    local, at, domain = cleaned.rpartition("@")
    if not at or not local or "." not in domain or len(cleaned) > 254 or any(c.isspace() for c in cleaned):
        return None
    return cleaned


class WebChatAuth:
    """Issue and verify sign-in links and sessions for one client environment."""

    def __init__(
        self,
        secret: bytes,
        spent_root: pathlib.Path,
        *,
        scope: str,
        allowed: Iterable[str],
        login_ttl_seconds: int = DEFAULT_LOGIN_TTL_SECONDS,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"web chat signing key must be at least {MIN_SECRET_BYTES} bytes")
        if not isinstance(scope, str) or not _SCOPE.match(scope):
            raise ValueError("scope must be a lowercase client-environment slug such as acme-prod")
        if not 60 <= login_ttl_seconds <= MAX_LOGIN_TTL_SECONDS:
            raise ValueError("sign-in link lifetime must be between 60 seconds and 1 hour")
        if not 300 <= session_ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError("session lifetime must be between 5 minutes and 24 hours")
        addresses = {_email(item) for item in allowed}
        if None in addresses:
            raise ValueError("every allowed sign-in address must be a valid email address")
        self._secret = bytes(secret)
        self._root = pathlib.Path(spent_root)
        self._scope = scope
        self._allowed = frozenset(addresses)
        self._login_ttl = login_ttl_seconds
        self._session_ttl = session_ttl_seconds
        self._clock = clock

    def _sign(self, kind: str, payload: str) -> str:
        # The kind is part of what is signed, so a sign-in token can never be accepted as a session.
        return _b64(hmac.new(self._secret, f"{kind}.{payload}".encode("ascii"), hashlib.sha256).digest())

    def _mint(self, kind: str, subject: str, ttl: int, **extra: Any) -> str:
        claims = {"k": kind, "s": self._scope, "u": subject, "x": int(self._clock()) + ttl, **extra}
        payload = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{self._sign(kind, payload)}"

    def _open(self, kind: str, token: Any) -> dict[str, Any]:
        try:
            payload, _, signature = str(token).strip().partition(".")
            if not payload or not signature or not hmac.compare_digest(signature, self._sign(kind, payload)):
                raise ChannelVerificationError(f"web chat {kind} token is not valid")
            claims = json.loads(_unb64(payload))
            subject, expires = claims["u"], claims["x"]
        except ChannelVerificationError:
            raise
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError) as exc:
            raise ChannelVerificationError(f"web chat {kind} token is not valid") from exc
        if (claims.get("k") != kind or claims.get("s") != self._scope or not isinstance(subject, str)
                or not isinstance(expires, int) or isinstance(expires, bool)):
            raise ChannelVerificationError(f"web chat {kind} token is not valid")
        if self._clock() >= expires:
            raise ChannelVerificationError(f"web chat {kind} token has expired")
        return claims

    def issue_login(self, address: str) -> str | None:
        """A single-use sign-in token for an allow-listed address, else ``None`` (never say which)."""
        normalized = _email(address)
        if normalized is None or normalized not in self._allowed:
            return None
        return self._mint("login", normalized, self._login_ttl, n=secrets.token_hex(12))

    def exchange(self, login_token: str) -> str:
        """Spend a sign-in token and return a session token; a second use is refused."""
        claims = self._open("login", login_token)
        if claims["u"] not in self._allowed:  # removed from the allow-list since the link was sent
            raise ChannelVerificationError("web chat sign-in is no longer permitted")
        nonce = claims.get("n")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{24}", nonce):
            raise ChannelVerificationError("web chat login token is not valid")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self._root / hashlib.sha256(nonce.encode()).hexdigest(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ChannelVerificationError("web chat sign-in link was already used") from exc
        os.close(descriptor)
        return self._mint("session", claims["u"], self._session_ttl)

    def session(self, token: str) -> ChannelIdentity:
        """The verified identity behind a session token; revoked by removing the address from the allow-list."""
        claims = self._open("session", token)
        if claims["u"] not in self._allowed:
            raise ChannelVerificationError("web chat session is no longer permitted")
        return ChannelIdentity(channel=CHANNEL, channel_user_id=claims["u"])


def _bearer(headers: Mapping[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "authorization":
            scheme, _, token = str(value).partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
    raise ChannelVerificationError("web chat request has no session")


def _json(body: bytes) -> dict[str, Any]:
    if not isinstance(body, (bytes, bytearray)) or not body or len(body) > MAX_BODY_BYTES:
        raise ChannelVerificationError("web chat request body is empty or too large")
    try:
        document = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ChannelVerificationError("web chat request body is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ChannelVerificationError("web chat request body is not a JSON object")
    return document


class WebChatAdapter:
    """``TextChannelAdapter`` for the built-in web chat. The page pushes replies through the injected transport."""

    channel = CHANNEL

    def __init__(self, auth: WebChatAuth, *, transport: Callable[[dict[str, Any]], str] | None = None) -> None:
        self._auth = auth
        self._transport = transport

    def parse_inbound(self, body: bytes, headers: Mapping[str, str]) -> InboundMessage:
        sender = self._auth.session(_bearer(headers))  # authority first; the body is untrusted input
        document = _json(body)
        conversation = document.get("conversationId")
        message_id = document.get("messageId")
        thread = document.get("threadId")
        text = document.get("text")
        if not (isinstance(conversation, str) and _SAFE_ID.match(conversation) and isinstance(message_id, str)
                and _SAFE_ID.match(message_id) and isinstance(text, str) and text.strip()):
            raise ChannelVerificationError("web chat message needs a conversation, a message id and text")
        if thread is not None and not (isinstance(thread, str) and _SAFE_ID.match(thread)):
            raise ChannelVerificationError("web chat thread id is not valid")
        return InboundMessage(
            channel=CHANNEL, conversation_id=conversation, thread_id=thread or conversation, message_id=message_id,
            sender=sender, text=text.strip()[:MAX_TEXT_CHARS],
        )

    def parse_approval_response(self, body: bytes, headers: Mapping[str, str]) -> ApprovalResponse | None:
        """A decision from the signed-in user's own click; who they are comes only from the session."""
        responder = self._auth.session(_bearer(headers))
        document = _json(body)
        request_id, decision = document.get("requestId"), document.get("decision")
        if request_id is None and decision is None:
            return None
        if not (isinstance(request_id, str) and _REQUEST_ID.match(request_id) and decision in ("approve", "reject")):
            raise ChannelVerificationError("web chat approval needs the exact request id and approve or reject")
        return ApprovalResponse(request_id=request_id, decision=decision, responder=responder)

    def _push(self, kind: str, conversation_id: str, thread_id: str | None, **fields: Any) -> str:
        if self._transport is None:
            raise RuntimeError("no web chat transport is configured for this deployment")
        return self._transport({"kind": kind, "conversationId": conversation_id, "threadId": thread_id or conversation_id, **fields})

    def send_reply(self, reply: OutboundReply) -> str:
        return self._push("reply", reply.conversation_id, reply.thread_id, text=reply.text[:MAX_TEXT_CHARS])

    def send_receipt(self, receipt: ActionReceipt) -> str:
        return self._push(
            "receipt", receipt.conversation_id, receipt.thread_id, requestId=receipt.request_id,
            text=f"{receipt.summary} ({receipt.tool}: {receipt.status}; ledger reference {receipt.request_id})",
        )

    def send_approval_prompt(self, prompt: ApprovalPrompt) -> str:
        return self._push(
            "approval", prompt.conversation_id, prompt.thread_id, requestId=prompt.request_id,
            text=f"Approval needed for {prompt.tool}: {prompt.summary}",
        )
