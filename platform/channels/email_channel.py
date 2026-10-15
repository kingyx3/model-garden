"""Email surface on the shared channel adapter contract (see ``text_channel.py``).

No mail provider or inbound address is built here; this module holds the parts that decide *who is
writing* and *whether an approval is genuine*, so every provider adapter shares them:

- **Sender verification.** Authority comes from the receiving mail server's own
  ``Authentication-Results`` header, never from the message text or an unauthenticated ``From``. Only
  a header carrying the deployment's trusted ``authserv-id`` counts (anything else is attacker
  controlled), exactly one such header must exist, and it must report ``dmarc=pass`` for the same
  domain as the single ``From`` address. DMARC pass already means SPF or DKIM passed *and aligned*.
  The verified address is then looked up in the ``PrincipalDirectory`` under channel ``email``; a
  sender that is not verified gets an ``unverified:`` identity that can never match a directory
  entry, so it is public and read-only like any unmapped channel user. The receiving MTA must remove
  inbound headers that already carry its own ``authserv-id`` (RFC 8601 section 5).
- **Threading.** Replies keep ``In-Reply-To`` and ``References`` so they land in the same thread.
- **Loop protection.** ``loop_risk`` names why a message must never get an automatic reply (an
  auto-reply, bulk or list mail, a bounce, a no-reply sender, or our own address or marker), outgoing
  replies are marked ``Auto-Submitted: auto-replied``, and ``ReplyLimiter`` caps replies per thread.
- **Approval links.** A signed, expiring link is bound to one ``requestId``, one decision and one
  approver address, and a request/approver pair can be consumed once: using either the approve or the
  reject link spends both. The link only proves control of the approver's mailbox; the approver must
  still be a mapped ``approver`` in the directory (``decide_from_channel``). Because mail scanners
  fetch links, the page behind a link must require a POST confirmation before the token is
  submitted here; never act on a GET.

Credential-free: this module opens no network connection and holds no provider secret except the
approval-link signing key the deployment supplies.
"""
from __future__ import annotations

import base64
import binascii
import email
import email.policy
import email.utils
import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import importlib.util
import sys


def _sibling(name: str, filename: str):
    """Load a sibling module by path once, so this works wherever the platform tree is copied."""
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

CHANNEL = "email"
UNVERIFIED_PREFIX = "unverified:"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 20000
MAX_ID_CHARS = 256
MAX_REFERENCES = 20
MIN_SECRET_BYTES = 32
DEFAULT_LINK_TTL_SECONDS = 24 * 60 * 60
MAX_LINK_TTL_SECONDS = 7 * 24 * 60 * 60
_NO_REPLY_LOCAL = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|bounces?|bounce[-_+].*|no[-_.]?reply[-_+].*)$",
    re.IGNORECASE,
)
_BULK_PRECEDENCE = frozenset({"bulk", "junk", "list", "auto_reply"})
OWN_MESSAGE_HEADER = "X-Model-Garden-Reply"
DEFAULT_THREAD_REPLY_LIMIT = 3
MAX_THREAD_REPLY_LIMIT = 20
_COMMENT = re.compile(r"\((?:[^()\\]|\\.)*\)")
_MESSAGE_ID = re.compile(r"<[^<>\s]{1,240}>")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


# -- sender verification ------------------------------------------------------------------


def parse_authentication_results(value: str) -> tuple[str, dict[str, dict[str, str]]]:
    """Parse one RFC 8601 header into ``(authserv-id, {method: {"result", "ptype.property": value}})``.

    Only the first result per method is kept. Comments are stripped. Anything unparseable yields an
    empty method set, which can never verify a sender.
    """
    cleaned = _COMMENT.sub(" ", value or "")
    parts = [part.strip() for part in cleaned.split(";")]
    if not parts or not parts[0]:
        return "", {}
    authserv_id = parts[0].split()[0].lower()
    methods: dict[str, dict[str, str]] = {}
    for part in parts[1:]:
        tokens = part.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, _, result = tokens[0].partition("=")
        method, result = method.lower(), result.lower()
        if not method or not result or method in methods:
            continue
        properties = {"result": result}
        for token in tokens[1:]:
            name, equals, item = token.partition("=")
            if equals and name:
                properties[name.lower()] = item.strip('"').lower()
        methods[method] = properties
    return authserv_id, methods


def _from_address(message: email.message.EmailMessage) -> str | None:
    headers = message.get_all("From") or []
    if len(headers) != 1:
        return None
    addresses = email.utils.getaddresses([str(headers[0])])
    if len(addresses) != 1:
        return None
    address = addresses[0][1].strip().lower()
    local, at, domain = address.rpartition("@")
    if not at or not local or not _DOMAIN.match(domain) or len(address) > 254:
        return None
    return address


def verified_sender(message: email.message.EmailMessage, trusted_authserv_id: str) -> str | None:
    """The sender's lowercase address when the trusted receiving server vouches for it, else ``None``."""
    trusted = (trusted_authserv_id or "").strip().lower()
    address = _from_address(message)
    if not trusted or address is None:
        return None
    ours = []
    for header in message.get_all("Authentication-Results") or []:
        authserv_id, methods = parse_authentication_results(str(header))
        if authserv_id == trusted:
            ours.append(methods)
    if len(ours) != 1:  # none, or ambiguous: fail closed
        return None
    dmarc = ours[0].get("dmarc")
    if not dmarc or dmarc.get("result") != "pass":
        return None
    if dmarc.get("header.from") != address.rpartition("@")[2]:
        return None
    return address


def loop_risk(message: email.message.EmailMessage, own_addresses: frozenset[str] = frozenset()) -> str | None:
    """Why this message must not receive an automatic reply, or ``None`` when a reply is allowed.

    Covers RFC 3834 auto-replies, bulk/list mail, delivery reports and null return paths, no-reply
    and mailer senders, our own addresses and our own reply marker. Checked in that order, so the
    reason names the strongest signal.
    """
    auto = str(message.get("Auto-Submitted") or "").strip().lower()
    if auto and auto.split(";")[0].strip() != "no":
        return "auto-submitted"
    if str(message.get("Precedence") or "").strip().lower() in _BULK_PRECEDENCE:
        return "bulk-precedence"
    if message.get("X-Auto-Response-Suppress") is not None:
        return "auto-response-suppressed"
    if any(message.get(name) is not None for name in ("List-Id", "List-Unsubscribe", "List-Post")):
        return "mailing-list"
    content_type = message.get_content_type()
    if content_type == "multipart/report" or str(message.get_param("report-type") or "").lower() == "delivery-status":
        return "bounce"
    if str(message.get("Return-Path") or "").strip() == "<>":
        return "bounce"
    if message.get(OWN_MESSAGE_HEADER) is not None:
        return "own-message"
    address = _from_address(message)
    if address is None:
        return "no-usable-sender"
    if address in own_addresses:
        return "own-address"
    if _NO_REPLY_LOCAL.match(address.rpartition("@")[0]):
        return "no-reply-sender"
    return None


class ReplyLimiter:
    """At most ``limit`` automatic replies per thread, counted under a file lock so restarts keep the count."""

    def __init__(self, root: pathlib.Path, limit: int = DEFAULT_THREAD_REPLY_LIMIT) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_THREAD_REPLY_LIMIT:
            raise ValueError(f"thread reply limit must be an integer from 1 to {MAX_THREAD_REPLY_LIMIT}")
        self._root = pathlib.Path(root)
        self._limit = limit

    def consume(self, thread_id: str) -> bool:
        """Count one reply to ``thread_id``; ``False`` (and nothing counted) once the limit is reached."""
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._root / hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            raw = os.read(descriptor, 16).decode("ascii", "ignore").strip()
            used = int(raw) if raw.isdigit() else 0
            if used >= self._limit:
                return False
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, str(used + 1).encode("ascii"))
            os.fsync(descriptor)
            return True
        finally:
            os.close(descriptor)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _single_id(value: str | None) -> str | None:
    match = _MESSAGE_ID.search(value or "")
    return match.group(0) if match else None


def _references(message: email.message.EmailMessage) -> list[str]:
    found = _MESSAGE_ID.findall(" ".join(str(item) for item in (message.get_all("References") or [])))
    return found[-MAX_REFERENCES:]


def _body_text(message: email.message.EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeDecodeError):
        return ""
    return text.strip()[:MAX_TEXT_CHARS] if isinstance(text, str) else ""


# -- approval links -----------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ApprovalLinks:
    """Issue and consume signed single-use approval tokens bound to a request, decision and approver."""

    def __init__(
        self,
        secret: bytes,
        spent_root: pathlib.Path,
        *,
        ttl_seconds: int = DEFAULT_LINK_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"approval link secret must be at least {MIN_SECRET_BYTES} bytes")
        if not 60 <= ttl_seconds <= MAX_LINK_TTL_SECONDS:
            raise ValueError("approval link lifetime must be between 60 seconds and 7 days")
        self._secret = bytes(secret)
        self._root = pathlib.Path(spent_root)
        self._ttl = ttl_seconds
        self._clock = clock

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest())

    @staticmethod
    def _pair(request_id: str, approver: str) -> str:
        return hashlib.sha256(f"{request_id}\0{approver}".encode("utf-8")).hexdigest()

    def issue(self, request_id: str, approver: str, decision: str) -> str:
        """A token for ``approver`` (an email address) to ``approve`` or ``reject`` ``request_id``."""
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{64}", request_id):
            raise ValueError("request id must be the 64-character hexadecimal ledger request id")
        normalized = (approver or "").strip().lower()
        if "@" not in normalized or len(normalized) > 254:
            raise ValueError("approver must be an email address")
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        payload = _b64(json.dumps(
            {"r": request_id, "a": normalized, "d": decision, "x": int(self._clock()) + self._ttl},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        return f"{payload}.{self._sign(payload)}"

    def consume(self, token: str) -> ApprovalResponse:
        """Verify and spend a token. Raises ``ChannelVerificationError`` for anything not genuine."""
        try:
            payload, _, signature = token.strip().partition(".")
            if not payload or not signature or not hmac.compare_digest(signature, self._sign(payload)):
                raise ChannelVerificationError("approval link is not valid")
            claims = json.loads(_unb64(payload))
            request_id, approver, decision, expires = claims["r"], claims["a"], claims["d"], claims["x"]
        except ChannelVerificationError:
            raise
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError) as exc:
            raise ChannelVerificationError("approval link is not valid") from exc
        if (
            not isinstance(request_id, str) or not isinstance(approver, str)
            or decision not in ("approve", "reject") or not isinstance(expires, int) or isinstance(expires, bool)
        ):
            raise ChannelVerificationError("approval link is not valid")
        if self._clock() >= expires:
            raise ChannelVerificationError("approval link has expired")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self._root / self._pair(request_id, approver), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ChannelVerificationError("approval link was already used") from exc
        os.close(descriptor)
        return ApprovalResponse(
            request_id=request_id, decision=decision, responder=ChannelIdentity(channel=CHANNEL, channel_user_id=approver)
        )


# -- adapter ------------------------------------------------------------------------------


def _header_safe(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value).strip()


class EmailChannelAdapter:
    """``TextChannelAdapter`` for email. Transport (SMTP or a mail API) is injected by the deployment."""

    channel = CHANNEL

    def __init__(
        self,
        trusted_authserv_id: str,
        *,
        approval_links: ApprovalLinks | None = None,
        transport: Callable[[dict[str, Any]], str] | None = None,
        own_addresses: tuple[str, ...] = (),
        reply_limiter: ReplyLimiter | None = None,
    ) -> None:
        if not (trusted_authserv_id or "").strip():
            raise ValueError("a trusted authserv-id is required to verify email senders")
        self._authserv_id = trusted_authserv_id.strip().lower()
        self._own = frozenset(item.strip().lower() for item in own_addresses if item.strip())
        self._limiter = reply_limiter
        self._links = approval_links
        self._transport = transport

    def _parse(self, body: bytes) -> email.message.EmailMessage:
        if not isinstance(body, (bytes, bytearray)) or not body or len(body) > MAX_MESSAGE_BYTES:
            raise ChannelVerificationError("inbound email is empty or too large")
        return email.message_from_bytes(bytes(body), policy=email.policy.default)  # type: ignore[return-value]

    def parse_inbound(self, body: bytes, headers: Mapping[str, str] | None = None) -> InboundMessage:
        """Normalise one raw RFC 5322 message. ``headers`` (provider transport metadata) is not trusted."""
        message = self._parse(body)
        message_id = _single_id(str(message.get("Message-ID") or ""))
        if message_id is None:
            raise ChannelVerificationError("inbound email has no usable Message-ID")
        address = verified_sender(message, self._authserv_id)
        user = address if address is not None else UNVERIFIED_PREFIX + _digest(str(message.get("From") or ""))
        references = _references(message)
        in_reply_to = _single_id(str(message.get("In-Reply-To") or ""))
        root = references[0] if references else (in_reply_to or message_id)
        attachments = tuple(
            Attachment(
                attachment_id=f"{message_id}#{index}",
                name=(part.get_filename() or "attachment")[:MAX_ID_CHARS],
                media_type=part.get_content_type(),
                size_bytes=len(part.get_payload(decode=True) or b""),
            )
            for index, part in enumerate(message.iter_attachments())
        )
        return InboundMessage(
            channel=CHANNEL, conversation_id=root, thread_id=root, message_id=message_id,
            sender=ChannelIdentity(channel=CHANNEL, channel_user_id=user),
            text=_body_text(message), attachments=attachments,
        )

    def parse_approval_response(self, body: bytes, headers: Mapping[str, str] | None = None) -> ApprovalResponse | None:
        """A decision only for a genuine, unspent, unexpired signed link token."""
        if self._links is None:
            return None
        token = bytes(body).decode("ascii", "ignore").strip() if body else ""
        return self._links.consume(token) if token else None

    def should_reply(self, body: bytes) -> tuple[bool, str | None]:
        """Whether the coworker may reply automatically, and the reason when not.

        Loop signals are checked first and never consume the per-thread allowance; only a reply that
        is allowed is counted against the thread's limit.
        """
        message = self._parse(body)
        reason = loop_risk(message, self._own)
        if reason is not None:
            return False, reason
        if self._limiter is not None:
            inbound = self.parse_inbound(body)
            if not self._limiter.consume(inbound.thread_id or inbound.conversation_id):
                return False, "thread-reply-limit"
        return True, None

    # -- outbound: composed here, sent by the injected transport ---------------------------

    @staticmethod
    def reply_headers(body: bytes, *, subject: str | None = None) -> dict[str, str]:
        """Headers that keep a reply in the sender's thread."""
        message = email.message_from_bytes(bytes(body), policy=email.policy.default)
        message_id = _single_id(str(message.get("Message-ID") or ""))
        chain = _references(message)
        if message_id:
            chain = [*chain, message_id][-MAX_REFERENCES:]
        original = _header_safe(subject if subject is not None else str(message.get("Subject") or ""))
        headers = {
            "Subject": original if re.match(r"(?i)^re:", original) else f"Re: {original}".strip(),
            "Auto-Submitted": "auto-replied",
            OWN_MESSAGE_HEADER: "1",
        }
        if message_id:
            headers["In-Reply-To"] = message_id
        if chain:
            headers["References"] = " ".join(chain)
        return headers

    def _send(self, message: dict[str, Any]) -> str:
        if self._transport is None:
            raise RuntimeError("no mail transport is configured for this deployment")
        return self._transport(message)

    def send_reply(self, reply: OutboundReply) -> str:
        return self._send({"kind": "reply", "conversationId": reply.conversation_id, "threadId": reply.thread_id, "text": reply.text})

    def send_receipt(self, receipt: ActionReceipt) -> str:
        return self._send({
            "kind": "receipt", "conversationId": receipt.conversation_id, "threadId": receipt.thread_id,
            "text": f"{receipt.summary} ({receipt.tool}: {receipt.status}; reference {receipt.request_id})",
        })

    def send_approval_prompt(self, prompt: ApprovalPrompt, *, approver: str | None = None) -> str:
        """Compose the prompt; with signing configured and an approver address, include both signed tokens."""
        message: dict[str, Any] = {
            "kind": "approval", "conversationId": prompt.conversation_id, "threadId": prompt.thread_id,
            "text": f"Approval needed for {prompt.tool}: {prompt.summary}", "requestId": prompt.request_id,
        }
        if self._links is not None and approver:
            message["approveToken"] = self._links.issue(prompt.request_id, approver, "approve")
            message["rejectToken"] = self._links.issue(prompt.request_id, approver, "reject")
        return self._send(message)
