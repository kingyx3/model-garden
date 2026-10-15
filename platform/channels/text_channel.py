"""One adapter contract for every text surface (Slack, Teams, email, built-in web chat).

The contract keeps two rules that every surface must share:

1. Authority comes from a *verified transport identity*, never from message text. An
   adapter verifies the provider's request signature and reports who sent the message
   (``ChannelIdentity``). The ``PrincipalDirectory`` maps that channel user to a client
   identity; anything the message *says* about who is writing, or who approved what, is
   only text.
2. Unmapped channel users get public, read-only behaviour by default. Governance denies
   them every non-read Tool, and they can never approve an action.

Approval responses are structured interactive payloads (buttons, adaptive-card actions,
signed links) carrying a request ID; the approver identity is the verified responder,
mapped through the same directory and holding the ``approver`` role.

Voice uses the same semantics: a LiveKit caller is an unverified channel user unless a
deployment maps a verified caller identity, so the voice Receptionist relies on
exact-action approval for writes. See ``docs/channels.md``.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

PUBLIC = "public"
MEMBER = "member"
APPROVER_ROLE = "approver"
DECISIONS = ("approve", "reject")
MAX_FIELD = 256


class ChannelVerificationError(PermissionError):
    """The inbound request could not be verified as coming from the channel provider."""


def _bounded(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD:
        raise ValueError(f"{label} must be a non-empty string of at most {MAX_FIELD} characters")
    return value.strip()


@dataclass(frozen=True)
class ChannelIdentity:
    """Who sent a message, as verified by the channel transport (not by message text)."""

    channel: str
    channel_user_id: str
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        _bounded(self.channel, "channel")
        _bounded(self.channel_user_id, "channel user id")
        if self.workspace_id is not None:
            _bounded(self.workspace_id, "channel workspace id")

    @property
    def key(self) -> str:
        scope = f"{self.workspace_id}/" if self.workspace_id else ""
        return f"{scope}{self.channel_user_id}"


@dataclass(frozen=True)
class Attachment:
    """A reference to a file; adapters fetch or send content only through governed Tools.

    On an outbound reply it is a *deliverable reference*: ``attachment_id`` is the artifact's location (for example
    ``tier0:acme-prod/a-0123456789ab``) and ``digest`` its SHA-256, so the thread shows what was produced and the ledger
    can be matched to it. No file bytes travel through this contract.
    """

    attachment_id: str
    name: str
    media_type: str
    size_bytes: int
    digest: str | None = None


@dataclass(frozen=True)
class InboundMessage:
    channel: str
    conversation_id: str
    thread_id: str | None
    message_id: str
    sender: ChannelIdentity
    text: str
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class OutboundReply:
    conversation_id: str
    thread_id: str | None
    text: str
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class ActionReceipt:
    """What the coworker tells a thread after a governed Tool call, with its ledger request ID."""

    conversation_id: str
    thread_id: str | None
    request_id: str
    tool: str
    status: str
    summary: str


@dataclass(frozen=True)
class ApprovalPrompt:
    conversation_id: str
    thread_id: str | None
    request_id: str
    tool: str
    summary: str


@dataclass(frozen=True)
class ApprovalResponse:
    request_id: str
    decision: str
    responder: ChannelIdentity

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError("approval decision must be approve or reject")


@dataclass(frozen=True)
class Principal:
    """The initiating identity governance sees for one inbound message."""

    identity: str
    channel: str
    access: str
    roles: frozenset[str] = field(default_factory=frozenset)
    mapped: bool = False

    @property
    def can_approve(self) -> bool:
        return self.mapped and self.access == MEMBER and APPROVER_ROLE in self.roles


def attachment_from_artifact(entry: Mapping[str, Any]) -> Attachment:
    """A deliverable reference from an artifact-store record (``artifacts.ArtifactStore.put``); never the file content."""
    return Attachment(
        attachment_id=str(entry["location"]), name=str(entry["name"]), media_type=str(entry["mediaType"]),
        size_bytes=int(entry["bytes"]), digest=str(entry["sha256"]),
    )


def describe_attachments(attachments: tuple[Attachment, ...]) -> list[str]:
    """One plain line per deliverable, so every surface says the same thing about a file."""
    lines = []
    for item in attachments[:10]:
        short = f", sha256 {item.digest[:12]}" if item.digest else ""
        lines.append(f"File ready: {item.name} ({item.media_type}, {item.size_bytes} bytes{short}), reference {item.attachment_id}")
    return lines


class TextChannelAdapter(Protocol):
    """Implemented once per surface; every method is transport-only."""

    channel: str

    def parse_inbound(self, body: bytes, headers: Mapping[str, str]) -> InboundMessage:
        """Verify the provider signature, then normalise; raise ChannelVerificationError."""

    def parse_approval_response(self, body: bytes, headers: Mapping[str, str]) -> ApprovalResponse | None:
        """Return a decision only for a verified structured interactive payload."""

    def send_reply(self, reply: OutboundReply) -> str: ...

    def send_receipt(self, receipt: ActionReceipt) -> str: ...

    def send_approval_prompt(self, prompt: ApprovalPrompt) -> str: ...


class PrincipalDirectory:
    """Map verified channel users to client identities from deployment configuration.

    Configuration shape (``EnvironmentBinding.spec.channels.directory``)::

        slack:
          T01/U123: {identity: alice@client.example, roles: [approver]}
        teams:
          <tenant id>/<Entra object id>: {identity: bob@client.example}
    """

    def __init__(self, mapping: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None) -> None:
        self._entries: dict[tuple[str, str], tuple[str, frozenset[str]]] = {}
        for channel, users in (mapping or {}).items():
            _bounded(channel, "directory channel")
            if not isinstance(users, Mapping):
                raise ValueError(f"directory for channel {channel!r} must be a mapping")
            for user_key, entry in users.items():
                _bounded(user_key, "directory channel user")
                if not isinstance(entry, Mapping):
                    raise ValueError(f"directory entry {channel}:{user_key} must be a mapping")
                identity = _bounded(entry.get("identity"), f"directory entry {channel}:{user_key} identity")
                roles = entry.get("roles", [])
                if not isinstance(roles, list) or not all(isinstance(role, str) and role for role in roles):
                    raise ValueError(f"directory entry {channel}:{user_key} roles must be a list of strings")
                self._entries[(channel, user_key)] = (identity, frozenset(roles))

    @classmethod
    def from_binding(cls, binding: Mapping[str, Any]) -> "PrincipalDirectory":
        spec = binding.get("spec", binding) if binding.get("kind") == "EnvironmentBinding" else binding
        channels = spec.get("channels") if isinstance(spec, Mapping) else None
        directory = channels.get("directory") if isinstance(channels, Mapping) else None
        return cls(directory if isinstance(directory, Mapping) else None)

    def resolve(self, sender: ChannelIdentity) -> Principal:
        entry = self._entries.get((sender.channel, sender.key))
        if entry is None:
            digest = hashlib.sha256(f"{sender.channel}\0{sender.key}".encode("utf-8")).hexdigest()[:16]
            return Principal(identity=f"unmapped:{sender.channel}:{digest}", channel=sender.channel, access=PUBLIC)
        identity, roles = entry
        return Principal(identity=identity, channel=sender.channel, access=MEMBER, roles=roles, mapped=True)

    def principal_for(self, message: InboundMessage) -> Principal:
        """Resolve from the verified sender only; ``message.text`` is never consulted."""
        if message.sender.channel != message.channel:
            raise ChannelVerificationError("sender channel does not match message channel")
        return self.resolve(message.sender)


def governance_for_principal(principal: Principal) -> tuple[str, dict[str, str], str]:
    """Return (initiating_user, governance context fields, principal_access) for governance."""
    return (
        principal.identity,
        {"originatingPrincipal": principal.identity, "channel": principal.channel},
        principal.access,
    )


def decide_from_channel(
    response: ApprovalResponse,
    directory: PrincipalDirectory,
    decide: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Apply a structured channel approval; the approver is the verified, mapped responder."""
    principal = directory.resolve(response.responder)
    if not principal.can_approve:
        raise PermissionError("channel user is not a mapped approver for this client")
    return decide(response.request_id, approved=response.decision == "approve", approver=principal.identity)
