"""Questions the coworker had no approved knowledge to answer, grouped so an owner can see what keeps coming up (#237).

When ``kb.search`` returns no documents the governed sidecar notes the question here. The aim is the owner's feedback loop (see what
customers ask that the business rules do not cover, then add an approved source through the existing knowledge review), not a transcript:

- **Bounded, redacted text.** A question is kept as one short sample (160 characters at most, the first one seen for its group) with
  email addresses, URLs, phone-like numbers and long identifiers masked. It is never used to answer anything and never enters the ledger.
- **Conservative grouping.** Two questions group only when they share the same set of significant words (a short stop list, light
  plural trimming, at most eight words), so different questions are not merged. Each group counts occurrences, remembers the latest time
  and the channels that asked, and has a status: ``open``, ``dismissed`` (an owner decided it needs nothing) or ``answered``.
- **Owner-only.** Nothing here is offered to the model or to customers. The operator command shows the samples; the inbox shows a count.
- **Expiry and bounds.** Groups not seen for ``retention_days`` are removed (``purge_expired``), the store holds at most ``MAX_GROUPS``
  groups (the oldest closed ones go first), and the ledger records only ids, counts and who changed a status.

One JSON document, rewritten atomically under an exclusive lock in a 0700 directory, scoped to one tenant. No live-deployment proof.
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
MAX_GROUPS = 500
MAX_SAMPLE = 160
MAX_WORDS = 8
STATUSES = ("open", "dismissed", "answered")
DEFAULT_RETENTION_DAYS = 90
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ID = re.compile(r"^q-[0-9a-f]{12}$")
_WORD = re.compile(r"[a-z][a-z'-]{2,}")
_STOP = frozenset("the and are for you your can does how what when where which who will with this that have has our their there they from "
                  "about please would could should any some get was were been let want need know tell".split())
_MASKS = ((re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"), (re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE), "[link]"),
          (re.compile(r"\b[A-Za-z0-9_-]{16,}\b"), "[id]"), (re.compile(r"\+?\d[\d\s().-]{5,}\d"), "[number]"))


class UnansweredError(ValueError):
    """The request is malformed or names a group that does not exist; the message is safe to show."""


def mask(text: str) -> str:
    for pattern, label in _MASKS:
        text = pattern.sub(label, text)
    return " ".join(text.split())[:MAX_SAMPLE]


def group_key(question: str) -> str | None:
    words = sorted({(word[:-1] if word.endswith("s") and len(word) > 3 else word) for word in _WORD.findall(question.lower()) if word not in _STOP})
    return hashlib.sha256(" ".join(words[:MAX_WORDS]).encode("utf-8")).hexdigest()[:12] if words else None


class UnansweredStore:
    def __init__(self, root: str | os.PathLike[str], *, tenant: str, audit: Callable[[str, str, dict[str, Any]], Any] | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise UnansweredError("a tenant is a short lowercase slug")
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.unanswered.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "groups": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise UnansweredError("the unanswered-question store has an unsupported format")
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

    def _note(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def record(self, question: str, *, channel: str | None, reason: str | None, now: int, agent: str | None = None) -> dict[str, Any] | None:
        """Note one unanswered question. Returns the group, or None when the text has no significant words to group by."""
        if not isinstance(question, str) or not question.strip():
            return None
        key = group_key(question)
        if key is None:
            return None
        group_id, label = f"q-{key}", re.sub(r"[^a-z0-9-]", "", str(channel or "unknown").lower())[:20] or "unknown"
        with self._locked() as state:
            group = state["groups"].get(group_id)
            if group is None:
                group = {"id": group_id, "sample": mask(question), "count": 0, "channels": {}, "firstAt": int(now), "latestAt": int(now),
                         "status": "open", "reason": "", "agent": (agent or "")[:63]}
                state["groups"][group_id] = group
            group["count"] += 1
            group["latestAt"] = int(now)
            group["channels"][label] = group["channels"].get(label, 0) + 1
            group["reason"] = re.sub(r"[^a-z0-9 -]", "", str(reason or "no-match").lower())[:40]
            if len(state["groups"]) > MAX_GROUPS:
                closed = sorted((g for g in state["groups"].values() if g["status"] != "open"), key=lambda g: g["latestAt"])
                for old in closed[: len(state["groups"]) - MAX_GROUPS]:
                    del state["groups"][old["id"]]
            return dict(group)

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise UnansweredError("status is open, dismissed or answered")
        with self._locked() as state:
            found = [dict(g) for g in state["groups"].values() if status is None or g["status"] == status]
        return sorted(found, key=lambda g: (-g["count"], -g["latestAt"], g["id"]))

    def open_summary(self, now: int) -> dict[str, int]:
        open_groups = self.list(status="open")
        return {"groups": len(open_groups), "questions": sum(g["count"] for g in open_groups),
                "oldestSeconds": max((max(0, now - g["firstAt"]) for g in open_groups), default=0)}

    def set_status(self, group_id: str, status: str, *, by: str, now: int) -> dict[str, Any]:
        if status not in STATUSES:
            raise UnansweredError("status is open, dismissed or answered")
        if not isinstance(group_id, str) or not _ID.match(group_id):
            raise UnansweredError("a group id looks like q-0123456789ab")
        if not isinstance(by, str) or not by.strip():
            raise UnansweredError("who is deciding is required")
        with self._locked() as state:
            group = state["groups"].get(group_id)
            if group is None:
                raise UnansweredError("no such group")
            before = group["status"]
            group.update(status=status, decidedBy=by.strip()[:128], decidedAt=int(now))
            result = dict(group)
        self._note("unanswered.status", by.strip(), {"group": group_id, "from": before, "to": status, "count": result["count"]})
        return result

    def purge_expired(self, now: int, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """Remove groups not seen for ``retention_days``; returns how many went."""
        cutoff = int(now) - retention_days * 86_400
        with self._locked() as state:
            stale = [group_id for group_id, g in state["groups"].items() if g["latestAt"] < cutoff]
            for group_id in stale:
                del state["groups"][group_id]
        if stale:
            self._note("unanswered.purged", "runtime:retention", {"groups": len(stale), "retentionDays": retention_days})
        return len(stale)
