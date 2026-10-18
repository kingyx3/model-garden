"""Scoped coworker memory that the client can inspect and delete.

This is the Model Garden contract for what a coworker remembers, not a memory engine: it is the part that makes
remembering safe to turn on, and a runtime's own memory (Hermes) is meant to sit behind it (#100 records that
decision as open). Nothing here is connected to a runtime or to chat commands yet.

- **Scopes.** ``client`` (everyone at this client), ``agent:<name>`` (one coworker) and
  ``conversation:<id>`` (one thread). A reader names the scopes it may see; an entry in a scope it did not name is
  invisible to it, so one conversation's memory never leaks into another's.
- **Never cross-tenant.** A store belongs to one tenant, held in its own file under a private directory; every
  record carries the tenant and a store refuses a record or operation for another.
- **Ledgered as digests.** Every write, delete and purge calls the supplied ``audit`` hook with identifiers and
  a SHA-256 digest of the text, never the text, so the ledger shows what was stored, by which Agent and from which
  conversation without holding personal data.
- **Restricted data is not remembered** unless the deployment's policy explicitly allows it, and card numbers are
  redacted from anything that is stored.
- **Owners delete their own; admins export and purge.** A person may list and delete entries they own (or that
  are about them); an administrator may export and purge per scope or for the whole client.

Files are mode 0600 in a 0700 directory, written atomically under an exclusive lock, and bounded in size and
count. Records are plaintext at rest on the sidecar-only volume, like the contact log.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
from collections.abc import Callable, Iterable, Iterator
from typing import Any

SCHEMA_VERSION = 1
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
MAX_TEXT = 500
MAX_ENTRIES = 2000
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SCOPE = re.compile(r"^(client|agent:[A-Za-z0-9][A-Za-z0-9._-]{0,63}|conversation:[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_CARD = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")
Audit = Callable[[str, str, dict[str, Any]], Any]


class MemoryStoreError(ValueError):
    """Raised for an invalid request, a refused write or an operation outside the caller's rights."""


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def redact_cards(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return "[REDACTED:card]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)

    return _CARD.sub(replace, text)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE.match(scope):
        raise MemoryStoreError("scope must be client, agent:<name> or conversation:<id>")
    return scope


def _principal(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PRINCIPAL.match(value):
        raise MemoryStoreError(f"{label} must be a short identity")
    return value


class MemoryStore:
    def __init__(self, root: pathlib.Path, *, tenant: str, audit: Audit | None = None, allow_restricted: bool = False) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise MemoryStoreError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.memory.json"
        self._audit = audit
        self._allow_restricted = bool(allow_restricted)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / f".{self.tenant}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "entries": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise MemoryStoreError("memory store has an unsupported format or belongs to another tenant")
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

    # -- writes -------------------------------------------------------------------------

    def remember(
        self,
        scope: str,
        text: str,
        *,
        agent: str,
        conversation_id: str | None = None,
        classification: str = "internal",
        owner: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Store one memory; the same text in the same scope is returned, not duplicated."""
        scope = check_scope(scope)
        agent = _principal(agent, "agent")
        if classification not in CLASSIFICATIONS:
            raise MemoryStoreError(f"classification must be one of {', '.join(CLASSIFICATIONS)}")
        if classification == "restricted" and not self._allow_restricted:
            raise MemoryStoreError("restricted data is not remembered unless this deployment's policy allows it")
        if not isinstance(text, str) or not text.strip():
            raise MemoryStoreError("a memory needs text")
        cleaned = redact_cards(text.strip())
        if len(cleaned) > MAX_TEXT:
            raise MemoryStoreError(f"a memory is at most {MAX_TEXT} characters")
        if owner is not None:
            owner = _principal(owner, "owner")
        if conversation_id is not None:
            conversation_id = _principal(conversation_id, "conversation id")
        if scope.startswith("conversation:") and conversation_id is None:
            conversation_id = scope.split(":", 1)[1]
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        text_digest = digest(cleaned)
        with self._locked() as state:
            entries = state["entries"]
            existing = next((entry for entry in entries.values() if entry["scope"] == scope and entry["digest"] == text_digest and entry.get("owner") == owner), None)
            if existing is not None:
                return {**existing, "duplicate": True}
            if len(entries) >= MAX_ENTRIES:
                raise MemoryStoreError("the memory is full; an administrator must purge entries")
            entry = {
                "id": "m-" + secrets.token_hex(6), "tenant": self.tenant, "scope": scope, "text": cleaned, "digest": text_digest,
                "classification": classification, "agent": agent, "createdAt": stamp,
                **({"owner": owner} if owner else {}),
                **({"conversationDigest": digest(conversation_id)} if conversation_id else {}),
            }
            entries[entry["id"]] = entry
        self._record("memory.write", f"agent:{agent}", {
            "memoryId": entry["id"], "scope": scope, "classification": classification, "textDigest": text_digest,
            **({"conversationDigest": entry["conversationDigest"]} if "conversationDigest" in entry else {}),
        })
        return {**entry, "duplicate": False}

    # -- reads --------------------------------------------------------------------------

    def list(self, visible_scopes: Iterable[str], *, owner: str | None = None) -> list[dict[str, Any]]:
        """Entries in the scopes the reader may see; with ``owner``, only entries that person owns."""
        scopes = {check_scope(item) for item in visible_scopes}
        with self._locked() as state:
            entries = [dict(entry) for entry in state["entries"].values() if entry["scope"] in scopes]
        if owner is not None:
            entries = [entry for entry in entries if entry.get("owner") == owner]
        return sorted(entries, key=lambda entry: (entry["createdAt"], entry["id"]))

    # -- deletion and administration ----------------------------------------------------

    def delete(self, memory_id: str, *, requested_by: str, admin: bool = False) -> dict[str, Any]:
        """Delete one entry. A person may delete what they own; an administrator may delete any."""
        who = _principal(requested_by, "requested_by")
        with self._locked() as state:
            entry = state["entries"].get(memory_id)
            if entry is None:
                raise MemoryStoreError("no such memory")
            if not admin and entry.get("owner") != who:
                raise MemoryStoreError("only the owner of a memory or an administrator may delete it")
            del state["entries"][memory_id]
        self._record("memory.delete", who, {"memoryId": memory_id, "scope": entry["scope"], "textDigest": entry["digest"], "admin": bool(admin)})
        return {"deleted": memory_id, "scope": entry["scope"]}

    def export(self, *, requested_by: str) -> dict[str, Any]:
        """Everything this client's coworker remembers, for an administrator."""
        who = _principal(requested_by, "requested_by")
        with self._locked() as state:
            entries = sorted((dict(entry) for entry in state["entries"].values()), key=lambda entry: (entry["createdAt"], entry["id"]))
        self._record("memory.export", who, {"entries": len(entries)})
        return {"tenant": self.tenant, "schemaVersion": SCHEMA_VERSION, "entries": entries}

    def purge(self, *, requested_by: str, scope: str | None = None) -> dict[str, int]:
        """Delete every entry, or every entry in one scope, for an administrator."""
        who = _principal(requested_by, "requested_by")
        wanted = check_scope(scope) if scope is not None else None
        with self._locked() as state:
            gone = [key for key, entry in state["entries"].items() if wanted is None or entry["scope"] == wanted]
            for key in gone:
                del state["entries"][key]
        self._record("memory.purge", who, {"entries": len(gone), "scope": wanted or "all"})
        return {"purged": len(gone)}
