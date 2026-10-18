"""One timeline per conversation, linked to a customer only on verified evidence (#115).

A caller who rings, then emails the next day, is one customer with two conversations. This is the credential-free core of
keeping that history coherent without merging two different people. It is **not a CRM** and holds no message text: a
conversation keeps *references* (a transcript reference, a message id, a ledger request id) and digests, so sensitive content
stays in its own governed storage and never lands here or in the Action Ledger. It is not wired to the voice or mail paths yet.

- **Provider-neutral ids.** ``cv-…`` identifies a conversation and is derived from the tenant, channel and the channel's own
  call or thread id, so redelivery, a retry and a restart reach the same conversation. An interaction's key comes from its
  kind and external id, so the same event is never stored twice. Tenant, channel, Agent, Agent version, task and trace ids are
  kept on the conversation.
- **Evidence, not identity.** A conversation collects ``ParticipantEvidence``: a kind (``email``, ``phone``, ``sso`` or ``name``),
  whether it is *verified* and its provenance. Only a digest of the normalised value is kept, never the address or number.
  A name, or anything a caller or sender merely stated, is *asserted* and can never link a customer.
- **Linking is deliberate.** ``resolve`` asks an injected ``directory(kind, digest)`` which customers hold each *verified*
  evidence digest. Exactly one customer across all of them makes a ``CustomerLink`` (confidence ``verified``); two or more
  different customers is a conflict and goes to the review queue, not to the first match; asserted evidence that matches a
  customer goes to the review queue for a person to confirm and never links by itself. The caller is never told a match
  exists, because that would confirm to an attacker that a number belongs to a customer.
- **One timeline.** ``conversation_timeline`` and ``customer_timeline`` return interactions ordered by when they happened (then
  by arrival), so a turn that arrives late still sits in the right place. A customer's timeline joins every conversation linked
  to them across channels; an unlinked conversation appears in no customer's timeline.
- **Private, bounded, erasable.** One file per tenant (0600 in a 0700 directory, atomic writes under a lock); another tenant's
  file is refused, so nothing joins across tenants. A customer can be erased with their linked conversations, a conversation
  alone can be erased, and ``purge_before`` applies a retention period. Audit events carry identifiers only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
CHANNELS = ("voice", "email", "chat", "handoff")
EVIDENCE_KINDS = ("email", "phone", "sso", "name")
LINKING_KINDS = ("email", "phone", "sso")
INTERACTION_KINDS = ("message", "call", "summary", "outcome", "action", "followup", "approval", "handoff")
MAX_CONVERSATIONS = 5000
MAX_INTERACTIONS = 500
MAX_EVIDENCE = 20
MAX_REVIEW = 1000
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EXTERNAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,255}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_REF = re.compile(r"^[a-z][a-z0-9-]*:[A-Za-z0-9._:/#=-]{1,200}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,59}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
Audit = Callable[[str, str, dict[str, Any]], Any]
Directory = Callable[[str, str], "list[str]"]


class ConversationError(ValueError):
    """Raised for an invalid record or a request outside the rules; nothing is changed when it is raised."""


def evidence_digest(kind: str, value: str) -> str:
    """The digest of a normalised email address or phone number (the only form in which evidence is kept)."""
    if kind not in EVIDENCE_KINDS or not isinstance(value, str) or not value.strip():
        raise ConversationError("evidence is a kind (email, phone, sso or name) and a non-empty value")
    text = value.strip().lower()
    if kind == "phone":
        digits = re.sub(r"\D", "", text)
        if len(digits) < 6:
            raise ConversationError("a phone number has at least 6 digits")
        text = digits.lstrip("0")
    elif kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ConversationError("an email evidence value is an address")
    return hashlib.sha256(f"{kind}\0{text}".encode("utf-8")).hexdigest()


def _check(value: Any, pattern: re.Pattern[str], message: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ConversationError(message)
    return value


class ConversationStore:
    def __init__(self, root: pathlib.Path, *, tenant: str, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise ConversationError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.conversations.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / f".{self.tenant}.conversations.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "conversations": {}, "links": {}, "review": {}, "arrivals": 0}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise ConversationError("conversation store has an unsupported format or belongs to another tenant")
                state = loaded
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                temporary = self.path.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(state, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def conversation_id(self, channel: str, external_id: str) -> str:
        if channel not in CHANNELS:
            raise ConversationError("channel must be one of " + ", ".join(CHANNELS))
        _check(external_id, _EXTERNAL, "the call or thread id must be a short identifier from the channel")
        return "cv-" + hashlib.sha256(f"{self.tenant}\0{channel}\0{external_id}".encode("utf-8")).hexdigest()[:20]

    # -- recording -----------------------------------------------------------------------

    def open(
        self,
        channel: str,
        external_id: str,
        *,
        agent: str,
        agent_version: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        environment: str | None = None,
        now: int,
    ) -> dict[str, Any]:
        """Open (or find) the conversation for a call or thread; opening again changes nothing."""
        identifier = self.conversation_id(channel, external_id)
        _check(agent, _NAME, "agent must be a short Agent name")
        optional = {"agentVersion": agent_version, "taskId": task_id, "traceId": trace_id, "environment": environment}
        for label, value in optional.items():
            if value is not None:
                _check(value, _EXTERNAL, f"{label} must be a short identifier")
        with self._locked() as state:
            existing = state["conversations"].get(identifier)
            if existing is not None:
                return {**existing, "duplicate": True}
            if len(state["conversations"]) >= MAX_CONVERSATIONS:
                raise ConversationError("the tenant has the maximum number of conversations")
            conversation = {
                "id": identifier, "tenant": self.tenant, "channel": channel, "externalDigest": hashlib.sha256(external_id.encode("utf-8")).hexdigest(),
                "agent": agent, "startedAt": now, "evidence": [], "interactions": [],
                **{key: value for key, value in optional.items() if value is not None},
            }
            state["conversations"][identifier] = conversation
        self._record("conversation.open", f"agent:{agent}", {"conversationId": identifier, "channel": channel, "agent": agent})
        return {**conversation, "duplicate": False}

    def add_evidence(self, conversation_id: str, kind: str, value: str, *, verified: bool, provenance: str) -> dict[str, Any]:
        """Attach evidence about who is on the other end. ``verified`` must be the channel's own finding, never a statement."""
        digest = evidence_digest(kind, value)
        _check(provenance, _LABEL, "provenance names how it was verified or stated, for example email-dkim or caller-asserted")
        if not isinstance(verified, bool) or (verified and kind == "name"):
            raise ConversationError("only an email, phone or sso identity can be verified; a name never is")
        with self._locked() as state:
            conversation = self._conversation(state, conversation_id)
            for item in conversation["evidence"]:
                if item["kind"] == kind and item["digest"] == digest:
                    if verified and not item["verified"]:
                        item.update(verified=True, provenance=provenance)
                    return dict(item)
            if len(conversation["evidence"]) >= MAX_EVIDENCE:
                raise ConversationError("a conversation holds at most %d pieces of evidence" % MAX_EVIDENCE)
            item = {"kind": kind, "digest": digest, "verified": verified, "provenance": provenance}
            conversation["evidence"].append(item)
        return dict(item)

    def add_interaction(
        self,
        conversation_id: str,
        *,
        kind: str,
        external_id: str,
        at: int,
        ref: str,
        speaker: str | None = None,
        label: str | None = None,
        request_id: str | None = None,
        content_digest: str | None = None,
    ) -> dict[str, Any]:
        """Add one immutable entry by *reference*; the same kind and external id is stored once, wherever it arrives."""
        if kind not in INTERACTION_KINDS:
            raise ConversationError("kind must be one of " + ", ".join(INTERACTION_KINDS))
        _check(external_id, _EXTERNAL, "the interaction's external id must be a short identifier")
        _check(ref, _REF, "ref is a reference such as transcript:abc123 or message:m-17, never the content")
        if not isinstance(at, int) or isinstance(at, bool) or at < 0:
            raise ConversationError("at is epoch seconds")
        entry: dict[str, Any] = {"kind": kind, "at": at, "ref": ref}
        if speaker is not None:
            entry["speaker"] = _check(speaker, _NAME, "speaker is a short label such as caller, agent or a person's identity")
        if label is not None:
            entry["label"] = _check(label, _LABEL, "label is a short outcome label, never a summary")
        if request_id is not None:
            entry["requestId"] = _check(request_id, _DIGEST, "requestId is a 64-character ledger request id")
        if content_digest is not None:
            entry["contentDigest"] = _check(content_digest, _DIGEST, "contentDigest is a SHA-256 hex digest")
        key = hashlib.sha256(f"{conversation_id}\0{kind}\0{external_id}".encode("utf-8")).hexdigest()[:24]
        with self._locked() as state:
            conversation = self._conversation(state, conversation_id)
            for existing in conversation["interactions"]:
                if existing["key"] == key:
                    return {**existing, "duplicate": True}
            if len(conversation["interactions"]) >= MAX_INTERACTIONS:
                raise ConversationError("a conversation holds at most %d interactions" % MAX_INTERACTIONS)
            state["arrivals"] = int(state.get("arrivals", 0)) + 1
            stored = {"key": key, "arrival": state["arrivals"], **entry}
            conversation["interactions"].append(stored)
        return {**stored, "duplicate": False}

    @staticmethod
    def _conversation(state: Mapping[str, Any], conversation_id: str) -> dict[str, Any]:
        conversation = state["conversations"].get(conversation_id)
        if conversation is None:
            raise ConversationError("no such conversation")
        return conversation

    # -- linking -------------------------------------------------------------------------

    def resolve(self, conversation_id: str, directory: Directory, *, now: int) -> dict[str, Any]:
        """Link the conversation to a customer if verified evidence names exactly one; otherwise queue it for a person.

        Returns ``{"state": "linked"|"review"|"unlinked", ...}``. A caller-facing reply must not depend on the state.
        """
        with self._locked() as state:
            conversation = self._conversation(state, conversation_id)
            if conversation_id in state["links"]:
                return {"state": "linked", "customerId": state["links"][conversation_id]["customerId"], "duplicate": True}
            verified = [item for item in conversation["evidence"] if item["verified"] and item["kind"] in LINKING_KINDS]
            asserted = [item for item in conversation["evidence"] if not item["verified"] or item["kind"] not in LINKING_KINDS]
            found: dict[str, list[str]] = {}
            for item in verified:
                for customer in directory(item["kind"], item["digest"]) or []:
                    found.setdefault(str(customer), []).append(item["kind"])
            if len(found) == 1:
                (customer, kinds), = found.items()
                state["links"][conversation_id] = {"customerId": customer, "confidence": "verified", "evidence": sorted(set(kinds)), "linkedAt": now}
                outcome = {"state": "linked", "customerId": customer, "confidence": "verified", "duplicate": False}
                audit = ("conversation.link", {"conversationId": conversation_id, "customerId": customer, "confidence": "verified", "evidence": sorted(set(kinds))})
            else:
                reason, candidates = None, sorted(found)
                if len(found) > 1:
                    reason = "conflicting-evidence"
                else:
                    for item in asserted:
                        if item["kind"] in LINKING_KINDS:  # stated, not verified: a person decides
                            matches = sorted({str(c) for c in directory(item["kind"], item["digest"]) or []})
                            if matches:
                                reason, candidates = "asserted-match", matches
                                break
                if reason is None:
                    return {"state": "unlinked"}
                review_id = "rv-" + hashlib.sha256(f"{conversation_id}\0{reason}".encode("utf-8")).hexdigest()[:16]
                if review_id not in state["review"]:
                    if len(state["review"]) >= MAX_REVIEW:
                        raise ConversationError("the review queue is full; clear it before more are added")
                    state["review"][review_id] = {"id": review_id, "conversationId": conversation_id, "reason": reason, "candidates": candidates, "createdAt": now}
                outcome = {"state": "review", "reviewId": review_id, "reason": reason}
                audit = ("conversation.review", {"conversationId": conversation_id, "reviewId": review_id, "reason": reason, "candidateCount": len(candidates)})
        self._record(audit[0], f"runtime:{self.tenant}", audit[1])
        return outcome

    def review_queue(self) -> list[dict[str, Any]]:
        with self._locked() as state:
            return sorted((dict(item) for item in state["review"].values()), key=lambda item: (item["createdAt"], item["id"]))

    def confirm(self, review_id: str, customer_id: str, *, by: str, now: int) -> dict[str, Any]:
        """A named person decides which customer a queued conversation belongs to (or one the queue did not offer)."""
        _check(by, _NAME, "by must be a named person")
        _check(customer_id, _EXTERNAL, "customer id must be a short identifier")
        with self._locked() as state:
            item = state["review"].pop(review_id, None)
            if item is None:
                raise ConversationError("no such review item")
            state["links"][item["conversationId"]] = {"customerId": customer_id, "confidence": f"confirmed-by:{by}", "evidence": [], "linkedAt": now}
        self._record("conversation.link", by, {"conversationId": item["conversationId"], "customerId": customer_id, "confidence": "confirmed"})
        return {"conversationId": item["conversationId"], "customerId": customer_id}

    def link(self, conversation_id: str, customer_id: str, *, by: str, now: int) -> dict[str, Any]:
        """A named person attaches a conversation to a customer directly, for one the review queue never held.

        A call whose transcript carries no number has no evidence to propose a match from, so without this it could never join a
        customer's history. It is a decision, not evidence: it is recorded as ``confirmed-by:<person>`` and ledgered, any open
        review item for the conversation is closed, and a conversation already linked to a *different* customer is refused
        (``unlink`` it first) so a person cannot silently re-point it.
        """
        _check(by, _NAME, "by must be a named person")
        _check(customer_id, _EXTERNAL, "customer id must be a short identifier")
        with self._locked() as state:
            self._conversation(state, conversation_id)
            existing = state["links"].get(conversation_id)
            if existing is not None and existing["customerId"] != customer_id:
                raise ConversationError("the conversation is already linked to a different customer; unlink it first")
            for review_id in [key for key, item in state["review"].items() if item["conversationId"] == conversation_id]:
                del state["review"][review_id]
            state["links"][conversation_id] = {"customerId": customer_id, "confidence": f"confirmed-by:{by}", "evidence": [], "linkedAt": now}
        self._record("conversation.link", by, {"conversationId": conversation_id, "customerId": customer_id, "confidence": "confirmed"})
        return {"conversationId": conversation_id, "customerId": customer_id}

    def unlink(self, conversation_id: str, *, by: str) -> None:
        """Take a conversation off a customer's timeline (a wrong match); the conversation itself stays."""
        _check(by, _NAME, "by must be a named person")
        with self._locked() as state:
            self._conversation(state, conversation_id)
            link = state["links"].pop(conversation_id, None)
        if link is None:
            raise ConversationError("the conversation is not linked to a customer")
        self._record("conversation.unlink", by, {"conversationId": conversation_id, "customerId": link["customerId"]})

    def list_conversations(self, *, unlinked_only: bool = False) -> list[dict[str, Any]]:
        """Conversations oldest first, as ids, channel, start time, size and link state; never evidence, text or references."""
        with self._locked() as state:
            rows = [
                {"id": item["id"], "channel": item["channel"], "startedAt": item["startedAt"], "interactions": len(item["interactions"]),
                 "linked": item["id"] in state["links"], **({"customerId": state["links"][item["id"]]["customerId"]} if item["id"] in state["links"] else {})}
                for item in state["conversations"].values()
            ]
        rows = [row for row in rows if not (unlinked_only and row["linked"])]
        return sorted(rows, key=lambda row: (row["startedAt"], row["id"]))

    def dismiss(self, review_id: str, *, by: str) -> None:
        _check(by, _NAME, "by must be a named person")
        with self._locked() as state:
            if state["review"].pop(review_id, None) is None:
                raise ConversationError("no such review item")
        self._record("conversation.review-dismissed", by, {"reviewId": review_id})

    # -- timelines -----------------------------------------------------------------------

    @staticmethod
    def _ordered(interactions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [dict(item) for item in sorted(interactions, key=lambda item: (item["at"], item["arrival"]))]

    def conversation_timeline(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._locked() as state:
            return self._ordered(list(self._conversation(state, conversation_id)["interactions"]))

    def customer_timeline(self, customer_id: str) -> list[dict[str, Any]]:
        """Every linked conversation's interactions in one ordered list, each naming its conversation and channel."""
        rows: list[dict[str, Any]] = []
        with self._locked() as state:
            for conversation_id, link in state["links"].items():
                if link["customerId"] != customer_id:
                    continue
                conversation = state["conversations"].get(conversation_id)
                if conversation is None:
                    continue
                for item in conversation["interactions"]:
                    rows.append({**item, "conversationId": conversation_id, "channel": conversation["channel"], "agent": conversation["agent"]})
        return self._ordered(rows)

    def link_of(self, conversation_id: str) -> dict[str, Any] | None:
        with self._locked() as state:
            link = state["links"].get(conversation_id)
            return dict(link) if link else None

    # -- erasure and retention -----------------------------------------------------------

    def _remove(self, state: dict[str, Any], conversation_ids: list[str]) -> int:
        for identifier in conversation_ids:
            state["conversations"].pop(identifier, None)
            state["links"].pop(identifier, None)
        gone = set(conversation_ids)
        for review_id in [key for key, item in state["review"].items() if item["conversationId"] in gone]:
            del state["review"][review_id]
        return len(gone)

    def erase_customer(self, customer_id: str, *, by: str) -> dict[str, int]:
        """Remove a customer's link and every conversation linked to them, across channels."""
        _check(by, _NAME, "by must be a named person")
        with self._locked() as state:
            ids = [cid for cid, link in state["links"].items() if link["customerId"] == customer_id]
            count = self._remove(state, ids)
        self._record("conversation.erase", by, {"customerId": customer_id, "conversations": count})
        return {"conversations": count}

    def erase_conversation(self, conversation_id: str, *, by: str) -> dict[str, int]:
        _check(by, _NAME, "by must be a named person")
        with self._locked() as state:
            count = self._remove(state, [conversation_id] if conversation_id in state["conversations"] else [])
        self._record("conversation.erase", by, {"conversationId": conversation_id, "conversations": count})
        return {"conversations": count}

    def purge_before(self, cutoff: int, *, by: str) -> dict[str, int]:
        """Retention: remove conversations that started before ``cutoff`` (epoch seconds)."""
        _check(by, _NAME, "by must be a named person")
        with self._locked() as state:
            ids = [cid for cid, item in state["conversations"].items() if item["startedAt"] < cutoff]
            count = self._remove(state, ids)
        self._record("conversation.purge", by, {"before": cutoff, "conversations": count})
        return {"conversations": count}

    def stats(self) -> dict[str, int]:
        with self._locked() as state:
            return {"conversations": len(state["conversations"]), "linked": len(state["links"]), "review": len(state["review"])}
