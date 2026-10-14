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

import json
import os
import pathlib
import re
import time
from typing import Any, Mapping

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


def _write(path: pathlib.Path, document: Mapping[str, Any]) -> None:
    _dir(path.parent)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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
        "assignee": _name(assignee, "assignee"),
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
    now: int | None = None,
) -> dict[str, Any]:
    """Close a pending hand-off as ``done`` or ``declined`` by a named person; single use."""
    if outcome not in OUTCOMES:
        raise HandoffError(f"outcome must be one of {', '.join(OUTCOMES)}")
    handoff_id = _check_id(handoff_id)
    person = _name(by, "completing person")
    pending_path = pathlib.Path(root) / "pending" / f"{handoff_id}.json"
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
    closed_path = pathlib.Path(root) / "closed" / f"{handoff_id}-{completed}.json"
    _write(closed_path, document)
    pending_path.unlink()
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
