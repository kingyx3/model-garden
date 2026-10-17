"""Tier 4 human hand-off store: prepare the exact action, route it to a named person, record the outcome.

When a capability is unbound or unavailable, or policy marks an action ``human-only``, the
governed boundary does not fail or execute. It prepares the exact action (the same request
material that approvals bind to) and stores it as a pending hand-off for a named person. The
person later marks it ``done`` or ``declined``; that identity and outcome are what the ledger
records, and the coworker may only report what the record says.

File layout under ``root`` (a sidecar-only volume, like the approval store):
``pending/<requestId>.json`` and ``closed/<requestId>-<completedAt>.json``. Records hold the
exact action arguments so a person can perform them, so they carry the same sensitivity as a
pending approval and are written 0600 in a 0700 directory.

Delivery to the person is the built-in inbox (operator CLI). Slack, Teams and email delivery
belong to the channel adapters and are not implemented here.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

SCHEMA_VERSION = 1
OUTCOMES = ("done", "declined")
REASONS = ("unbound", "unavailable", "human-only")
REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
MAX_NAME = 256
MAX_NOTE = 500
DEFAULT_DEADLINE_HOURS = 24
MAX_DEADLINE_HOURS = 24 * 30


class HandoffError(ValueError):
    """Raised for an invalid, unknown or already-closed hand-off."""


def _dir(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


@contextmanager
def _exclusive(root: pathlib.Path) -> Iterator[None]:
    """Serialize every mutation of a tenant's hand-off store, including across worker processes.

    Keep the lock in the root (not pending/): a completion moves a record between directories.
    This runtime uses Linux/POSIX filesystem locking; the lock file is never removed.
    """
    folder = _dir(pathlib.Path(root))
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(folder / ".lock", flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _write(path: pathlib.Path, document: Mapping[str, Any]) -> None:
    """Private atomic replacement: never share a temporary filename with another writer."""
    _dir(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary = pathlib.Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HandoffError(f"{path.name}: expected a JSON object")
    return value


def _check_id(handoff_id: str) -> str:
    if not isinstance(handoff_id, str) or not REQUEST_ID.fullmatch(handoff_id):
        raise HandoffError("hand-off id must be a 64-character lowercase SHA-256 hex value")
    return handoff_id


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"{label} must be a non-empty string")
    if len(value.strip()) > MAX_NAME:
        raise HandoffError(f"{label} exceeds {MAX_NAME} characters")
    return value.strip()


def create(
    root: pathlib.Path,
    request: Mapping[str, Any],
    *,
    capability: str | None,
    reason: str,
    assignee: str,
    deadline_hours: int = DEFAULT_DEADLINE_HOURS,
    now: int | None = None,
) -> dict[str, Any]:
    """Store a pending hand-off for ``request``; an identical pending one is returned unchanged."""
    if reason not in REASONS:
        raise HandoffError(f"reason must be one of {', '.join(REASONS)}")
    if not isinstance(deadline_hours, int) or isinstance(deadline_hours, bool) or not 1 <= deadline_hours <= MAX_DEADLINE_HOURS:
        raise HandoffError(f"deadline_hours must be an integer from 1 to {MAX_DEADLINE_HOURS}")
    handoff_id = _check_id(request.get("requestId", ""))
    pending_path = pathlib.Path(root) / "pending" / f"{handoff_id}.json"
    person = _name(assignee, "assignee")
    with _exclusive(root):
        if pending_path.is_file():
            return _read(pending_path)
        created = int(time.time()) if now is None else now
        document: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "id": handoff_id,
            "status": "pending",
            "reason": reason,
            "capability": capability,
            "tool": request["tool"],
            "assignee": person,
            "channel": "inbox",
            "createdAt": created,
            "deadline": created + deadline_hours * 3600,
            "request": dict(request),
        }
        _write(pending_path, document)
        return document


def pending(root: pathlib.Path) -> list[dict[str, Any]]:
    folder = pathlib.Path(root) / "pending"
    documents = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        document = _read(path)
        if document.get("id") != path.stem or not REQUEST_ID.fullmatch(path.stem):
            raise HandoffError(f"unsafe pending hand-off record: {path.name}")
        documents.append(document)
    return documents


def closed(root: pathlib.Path) -> list[dict[str, Any]]:
    folder = pathlib.Path(root) / "closed"
    return [_read(path) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def find(root: pathlib.Path, handoff_id: str) -> dict[str, Any] | None:
    """The pending record, else the most recently closed one for this exact action."""
    handoff_id = _check_id(handoff_id)
    path = pathlib.Path(root) / "pending" / f"{handoff_id}.json"
    if path.is_file():
        return _read(path)
    history = sorted(
        (item for item in closed(root) if item.get("id") == handoff_id),
        key=lambda item: item.get("completedAt", 0),
    )
    return history[-1] if history else None


def complete(
    root: pathlib.Path,
    handoff_id: str,
    outcome: str,
    by: str,
    *,
    note: str | None = None,
    via: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Close a pending hand-off as ``done`` or ``declined`` by a named person; single use.

    ``via`` names the channel the person answered through when it was not the operator command (``slack``, ``email``…).
    """
    if outcome not in OUTCOMES:
        raise HandoffError(f"outcome must be one of {', '.join(OUTCOMES)}")
    handoff_id = _check_id(handoff_id)
    person = _name(by, "completing person")
    pending_path = pathlib.Path(root) / "pending" / f"{handoff_id}.json"
    channel = _name(via, "reply channel") if via is not None else None
    with _exclusive(root):
        if not pending_path.is_file():
            raise HandoffError(f"no pending hand-off exists for {handoff_id}")
        document = _read(pending_path)
        completed = int(time.time()) if now is None else now
        document.update(
            status=outcome,
            completedBy=person,
            completedAt=completed,
            late=completed > int(document.get("deadline", completed)),
        )
        if note and note.strip():
            document["note"] = note.strip()[:MAX_NOTE]
        if channel is not None:
            document["completedVia"] = channel
        # Reopening and closing again in the same second must not overwrite an earlier receipt.
        closed_path = pathlib.Path(root) / "closed" / f"{handoff_id}-{completed}-{uuid.uuid4().hex}.json"
        _write(closed_path, document)
        pending_path.unlink()
        return document


NOTICE_KINDS = ("assigned", "reminder", "escalation")
MIN_REMINDER_LEAD_SECONDS = 30 * 60
MAX_REMINDER_LEAD_SECONDS = 2 * 60 * 60


def _utc(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def due_notices(
    root: pathlib.Path, *, now: int | None = None, escalate_to: str | None = None
) -> list[dict[str, Any]]:
    """Notices that should reach a person now, one per hand-off and kind, until recorded as sent.

    - ``assigned``: the named person has not yet been told it exists;
    - ``reminder``: shortly before the deadline (a quarter of the window, between 30 minutes and
      2 hours), while it is still open and the person has not acknowledged it;
    - ``escalation``: once the deadline has passed, to ``escalate_to`` when given, else to the assignee
      again. A hand-off is never silently dropped.

    A notice names the Tool, the deadline and the reference, never the action's arguments, so it is safe
    for any channel; the person opens the exact content through the operator command. Nothing is sent
    here: the caller delivers each notice through a channel and then calls :func:`mark_notified`, so a
    failed delivery is offered again (at-least-once).
    """
    current = int(time.time()) if now is None else now
    escalation_target = _name(escalate_to, "escalation contact") if escalate_to else None
    notices: list[dict[str, Any]] = []
    for document in pending(root):
        sent = document.get("notices") if isinstance(document.get("notices"), dict) else {}
        deadline, created = int(document["deadline"]), int(document.get("createdAt", current))
        overdue = current > deadline
        lead = min(MAX_REMINDER_LEAD_SECONDS, max(MIN_REMINDER_LEAD_SECONDS, (deadline - created) // 4))
        wanted: list[tuple[str, str]] = []
        if "assigned" not in sent:
            wanted.append(("assigned", document["assignee"]))
        acknowledged = isinstance(document.get("acknowledged"), dict)
        if "reminder" not in sent and not acknowledged and not overdue and current >= deadline - lead:
            wanted.append(("reminder", document["assignee"]))
        if "escalation" not in sent and overdue:
            wanted.append(("escalation", escalation_target or document["assignee"]))
        for kind, recipient in wanted:
            tool, reference = document["tool"], document["id"][:12]
            if kind == "assigned":
                text = (f"{tool} is waiting for you: the coworker prepared it but cannot perform it. "
                        f"Please do it by {_utc(deadline)} and mark it done or declined. Reference {reference}.")
            elif kind == "reminder":
                text = f"Reminder: {tool} is still waiting for you and is due by {_utc(deadline)}. Reference {reference}."
            else:
                text = (f"OVERDUE: {tool} was due by {_utc(deadline)} and has not been done or declined "
                        f"(assigned to {document['assignee']}). Reference {reference}.")
            notices.append({
                "handoffId": document["id"], "kind": kind, "to": recipient, "tool": tool,
                "deadline": deadline, "overdue": overdue, "text": text,
            })
    return notices


def mark_notified(
    root: pathlib.Path, handoff_id: str, kind: str, *, via: str, now: int | None = None
) -> dict[str, Any]:
    """Record that a notice was delivered (and through which channel); a repeat is a no-op."""
    if kind not in NOTICE_KINDS:
        raise HandoffError(f"notice kind must be one of {', '.join(NOTICE_KINDS)}")
    handoff_id = _check_id(handoff_id)
    channel = _name(via, "delivery channel")
    path = pathlib.Path(root) / "pending" / f"{handoff_id}.json"
    with _exclusive(root):
        if not path.is_file():
            raise HandoffError(f"no pending hand-off exists for {handoff_id}")
        document = _read(path)
        notices = document.get("notices") if isinstance(document.get("notices"), dict) else {}
        if kind in notices:
            return document
        notices[kind] = {"at": int(time.time()) if now is None else now, "via": channel}
        document["notices"] = notices
        _write(path, document)
        return document


def acknowledge(
    root: pathlib.Path, handoff_id: str, by: str, *, via: str, now: int | None = None
) -> dict[str, Any]:
    """Record that a named person has seen a pending hand-off and has it in hand; a repeat is a no-op.

    An acknowledgement only quiets the pre-deadline ``reminder``. It never closes the hand-off and never prevents the
    ``escalation`` once the deadline passes: seen is not done.
    """
    handoff_id = _check_id(handoff_id)
    person, channel = _name(by, "acknowledging person"), _name(via, "reply channel")
    path = pathlib.Path(root) / "pending" / f"{handoff_id}.json"
    with _exclusive(root):
        if not path.is_file():
            raise HandoffError(f"no pending hand-off exists for {handoff_id}")
        document = _read(path)
        if isinstance(document.get("acknowledged"), dict):
            return document
        document["acknowledged"] = {"by": person, "at": int(time.time()) if now is None else now, "via": channel}
        _write(path, document)
        return document


def describe(document: Mapping[str, Any], *, now: int | None = None) -> dict[str, Any]:
    """The only facts the coworker may report, with a plain-language statement."""
    status = document.get("status")
    tool = document.get("tool")
    who = document.get("assignee")
    current = int(time.time()) if now is None else now
    if status == "pending":
        overdue = current > int(document.get("deadline", current))
        message = (
            f"I cannot perform {tool} myself here. I have prepared the exact action for {who} to do. "
            "It has NOT been done yet"
            + (" and the requested time has passed." if overdue else ".")
        )
    elif status == "done":
        message = f"{document.get('completedBy')} confirmed that they carried out {tool}. I did not do it myself."
    elif status == "declined":
        message = f"{document.get('completedBy')} declined {tool}. It was NOT done."
    else:
        raise HandoffError(f"unknown hand-off status {status!r}")
    return {
        "handoffId": document.get("id"),
        "status": status,
        "tool": tool,
        "assignee": who,
        "deadline": document.get("deadline"),
        "performedBy": document.get("completedBy") if status == "done" else None,
        "message": message,
    }
