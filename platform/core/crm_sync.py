"""Per-interaction CRM sync states with idempotency keys, retry with backoff and reconciliation.

Filing a call or email to a CRM must survive rate limits, outages, redelivered webhooks and restarts without ever
duplicating a record, and must never claim success the provider did not confirm (#115). This module is the
provider-neutral delivery ledger for that: it does not talk to a CRM and has no provider code. A connector (or
the built-in log) does the write; this records what is known about each interaction.

States: ``pending`` (not yet acknowledged), ``synced`` (the provider acknowledged and returned a reference),
``failed`` (a transient error; retried after a backoff), ``blocked`` (a permanent error, or too many attempts; a
person must repair it), ``reconciled`` (a lookup found the record already at the provider, so it was linked and not
written twice).

- **Stable idempotency key.** Derived from the tenant, channel, external call or message id and interaction type,
  not from content or time, so a redelivered webhook, a retry and a restarted worker all land on the same entry.
  A provider that supports idempotent writes is given the same key.
- **No success without acknowledgement.** ``synced`` and ``reconciled`` need a non-empty provider reference.
- **Backoff and a ceiling.** Transient errors (``rate-limited``, ``outage``) retry after 30 s, doubling to 1 h;
  after 8 attempts the entry is ``blocked``. ``rejected`` and ``unauthorized`` block at once.
- **A repair action** in plain language accompanies every state that needs a person.
- **Linked to its conversation.** An entry can carry the ``cv-…`` id of the cross-channel conversation (``conversations.py``) it
  filed, so a failed or blocked filing is found from the timeline and the other way round (``list(conversation_id=...)``). The id
  is a reference, not part of the idempotency key; a redelivery can attach a missing one but never replaces one already there.
- **Digests in the ledger.** Every transition calls the audit hook with identifiers, state, attempt count and the
  error code, never interaction content. One tenant per private file, bounded in size.

Cross-channel timelines, identity linkage and moving to a different CRM are not part of this module.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Iterator
from typing import Any

SCHEMA_VERSION = 1
STATES = ("pending", "synced", "failed", "blocked", "reconciled")
TRANSIENT = ("rate-limited", "outage")
PERMANENT = ("rejected", "unauthorized")
ERROR_CODES = TRANSIENT + PERMANENT
MAX_ATTEMPTS = 8
BACKOFF_FIRST = 30
BACKOFF_CEILING = 3600
MAX_ENTRIES = 20000
CHANNELS = ("voice", "email", "chat", "handoff")
INTERACTION_TYPES = ("call", "email", "chat")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EXTERNAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/#@<>+=-]{0,255}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/#@-]{0,255}$")
_CONVERSATION = re.compile(r"^cv-[0-9a-f]{20}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
Audit = Callable[[str, str, dict[str, Any]], Any]
REPAIRS = {
    "pending": "Waiting for the first delivery attempt; nothing to do.",
    "failed": "The CRM was temporarily unavailable or rate-limiting; it will be retried automatically.",
    "blocked-rejected": "The CRM refused this record. Check the contact and field mapping, then ask an operator to retry it.",
    "blocked-unauthorized": "The CRM connection is not authorised. Reconnect the CRM, then ask an operator to retry it.",
    "blocked-attempts": "Delivery kept failing. Check the CRM connection and status, then ask an operator to retry it.",
}


class SyncError(ValueError):
    """Raised for an invalid transition or input; never carries interaction content."""


def idempotency_key(tenant: str, channel: str, external_id: str, interaction_type: str) -> str:
    """The same call or message always yields the same key, whatever the provider or retry path."""
    if channel not in CHANNELS:
        raise SyncError(f"channel must be one of {', '.join(CHANNELS)}")
    if interaction_type not in INTERACTION_TYPES:
        raise SyncError(f"interaction type must be one of {', '.join(INTERACTION_TYPES)}")
    if not isinstance(external_id, str) or not _EXTERNAL.match(external_id):
        raise SyncError("external id must be the call or message identifier (letters, digits and : . _ / # @ < > + = -)")
    raw = json.dumps([tenant, channel, external_id, interaction_type], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_CEILING, BACKOFF_FIRST * 2 ** max(0, attempts - 1))


def describe(entry: dict[str, Any]) -> dict[str, Any]:
    """What may be said about this interaction: its state, the provider reference if any, and what a person should do."""
    state = entry["state"]
    repair_key = state if state in ("pending", "failed") else (f"blocked-{entry['lastError']}" if entry.get("lastError") in PERMANENT else "blocked-attempts")
    synced = state in ("synced", "reconciled")
    return {
        "key": entry["key"], "channel": entry["channel"], "interactionType": entry["interactionType"], "state": state,
        "attempts": entry["attempts"], "providerRef": entry.get("providerRef"),
        **({"conversationId": entry["conversationId"]} if entry.get("conversationId") else {}),
        "repair": None if synced else REPAIRS[repair_key],
        "message": (f"Filed to the CRM (reference {entry['providerRef']})." if synced else "Not confirmed by the CRM yet; I cannot say it was filed."),
    }


def summary(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    for entry in entries:
        counts[entry["state"]] += 1
    return counts


class SyncLedger:
    def __init__(self, root: pathlib.Path, *, tenant: str, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise SyncError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.crm-sync.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / f".{self.tenant}.crm-sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "entries": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise SyncError("sync ledger has an unsupported format or belongs to another tenant")
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

    def _record(self, action: str, by: str, entry: dict[str, Any], **extra: Any) -> None:
        if self._audit is not None:
            self._audit(action, by, {"key": entry["key"], "channel": entry["channel"], "state": entry["state"],
                                     "attempts": entry["attempts"], **({"conversationId": entry["conversationId"]} if entry.get("conversationId") else {}),
                                     **({"error": entry["lastError"]} if entry.get("lastError") else {}), **extra})

    def enqueue(self, *, channel: str, external_id: str, interaction_type: str, now: int, conversation_id: str | None = None) -> dict[str, Any]:
        """Register an interaction for filing; the same call or message returns the existing entry, unchanged.

        ``conversation_id`` links the entry to its conversation; a repeat attaches it only if the entry has none yet.
        """
        key = idempotency_key(self.tenant, channel, external_id, interaction_type)
        if conversation_id is not None and (not isinstance(conversation_id, str) or not _CONVERSATION.match(conversation_id)):
            raise SyncError("a conversation id is the cv- identifier from the conversation store")
        with self._locked() as state:
            entries = state["entries"]
            if key in entries:
                if conversation_id and not entries[key].get("conversationId"):
                    entries[key]["conversationId"] = conversation_id
                return {**entries[key], "duplicate": True}
            if len(entries) >= MAX_ENTRIES:
                raise SyncError("the sync ledger is full")
            entry = {"key": key, "tenant": self.tenant, "channel": channel, "interactionType": interaction_type,
                     "externalId": external_id, "state": "pending", "attempts": 0, "createdAt": now, "nextAttemptAt": now,
                     **({"conversationId": conversation_id} if conversation_id else {})}
            entries[key] = entry
        self._record("crm-sync.enqueue", "runtime:crm-sync", entry)
        return {**entry, "duplicate": False}

    def get(self, key: str) -> dict[str, Any] | None:
        with self._locked() as state:
            entry = state["entries"].get(key)
            return dict(entry) if entry else None

    def list(self, *, states: tuple[str, ...] | None = None, conversation_id: str | None = None) -> list[dict[str, Any]]:
        with self._locked() as state:
            entries = [dict(entry) for entry in state["entries"].values()]
        if states is not None:
            entries = [entry for entry in entries if entry["state"] in states]
        if conversation_id is not None:
            entries = [entry for entry in entries if entry.get("conversationId") == conversation_id]
        return sorted(entries, key=lambda entry: (entry["createdAt"], entry["key"]))

    def due(self, *, now: int) -> list[dict[str, Any]]:
        """Entries a worker should attempt now: pending or failed, past their backoff. Blocked and filed ones never."""
        return [entry for entry in self.list(states=("pending", "failed")) if entry["nextAttemptAt"] <= now]

    def _transition(self, key: str, change: Callable[[dict[str, Any]], str], by: str) -> dict[str, Any]:
        with self._locked() as state:
            entry = state["entries"].get(key)
            if entry is None:
                raise SyncError("no such interaction")
            action = change(entry)
            result = dict(entry)
        self._record(action, by, result)
        return result

    def record_success(self, key: str, *, provider_ref: str, now: int) -> dict[str, Any]:
        """The provider acknowledged the write. A reference is required: no acknowledgement, no ``synced``."""
        if not isinstance(provider_ref, str) or not _REF.match(provider_ref):
            raise SyncError("a successful sync needs the provider's reference for the record")

        def change(entry: dict[str, Any]) -> str:
            if entry["state"] in ("synced", "reconciled"):
                if entry.get("providerRef") != provider_ref:
                    raise SyncError("this interaction is already filed under a different provider reference")
                return "crm-sync.confirmed"
            if entry["state"] == "blocked":
                raise SyncError("a blocked interaction must be retried by a person before it can sync")
            entry.update(state="synced", attempts=entry["attempts"] + 1, providerRef=provider_ref, syncedAt=now)
            entry.pop("lastError", None)
            return "crm-sync.synced"

        return self._transition(key, change, "runtime:crm-sync")

    def record_failure(self, key: str, *, error_code: str, now: int) -> dict[str, Any]:
        """A failed attempt. Transient errors back off and retry; permanent ones, or too many attempts, block."""
        if error_code not in ERROR_CODES:
            raise SyncError(f"error code must be one of {', '.join(ERROR_CODES)}")

        def change(entry: dict[str, Any]) -> str:
            if entry["state"] in ("synced", "reconciled"):
                raise SyncError("this interaction is already filed")
            if entry["state"] == "blocked":
                raise SyncError("a blocked interaction must be retried by a person before another attempt")
            attempts = entry["attempts"] + 1
            entry.update(attempts=attempts, lastError=error_code, lastAttemptAt=now)
            if error_code in PERMANENT or attempts >= MAX_ATTEMPTS:
                entry["state"] = "blocked"
                return "crm-sync.blocked"
            entry["state"] = "failed"
            entry["nextAttemptAt"] = now + backoff_seconds(attempts)
            return "crm-sync.failed"

        return self._transition(key, change, "runtime:crm-sync")

    def reconcile(self, key: str, *, provider_ref: str, now: int) -> dict[str, Any]:
        """A lookup by idempotency key found the record already at the provider: link it, never write it again."""
        if not isinstance(provider_ref, str) or not _REF.match(provider_ref):
            raise SyncError("reconciliation needs the provider's reference for the existing record")

        def change(entry: dict[str, Any]) -> str:
            if entry["state"] == "synced":
                raise SyncError("this interaction is already filed")
            entry.update(state="reconciled", providerRef=provider_ref, syncedAt=now)
            entry.pop("lastError", None)
            return "crm-sync.reconciled"

        return self._transition(key, change, "runtime:crm-sync")

    def retry(self, key: str, *, by: str, now: int) -> dict[str, Any]:
        """A named person repaired the cause: a blocked or failed interaction becomes pending again."""
        if not isinstance(by, str) or not _PERSON.match(by):
            raise SyncError("a retry names the person who repaired it")

        def change(entry: dict[str, Any]) -> str:
            if entry["state"] not in ("blocked", "failed"):
                raise SyncError("only a blocked or failed interaction can be retried")
            entry.update(state="pending", attempts=0, nextAttemptAt=now)
            entry.pop("lastError", None)
            return "crm-sync.retry"

        return self._transition(key, change, by)
