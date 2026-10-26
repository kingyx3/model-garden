#!/usr/bin/env python3
"""List and decide exact governed runtime actions without a control plane.

An approval is for *now*: a pending request and a recorded decision both expire (default 24 hours, ``MODEL_GARDEN_APPROVAL_TTL_HOURS``
1-720), so a decision given long ago can never authorise an identical request made much later. A request records when it was asked
(``requestedAt``) and a decision when it was made (``decidedAt``); a record without one (made before this existed) is aged by its file
time. An expired request cannot be decided and is not listed, and an expired decision is discarded by the governed boundary instead of
being used (``sweep`` removes both).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from typing import Any

REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 720


def _load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """Publish a complete decision once, without replacing an earlier approver's choice.

    The private temporary file is fsynced before a same-directory hard link
    atomically claims the final name. A second writer (or existing symlink)
    fails with EEXIST; no read-then-write race can reverse an approval.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            raise ValueError("this exact-action request has already been decided") from None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


def _validate_request_id(request_id: str) -> str:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("request id must be a 64-character lowercase SHA-256 hex value")
    return request_id


def ttl_seconds(environ: Mapping[str, str] | None = None) -> int:
    """How long an approval request or decision stays valid, from ``MODEL_GARDEN_APPROVAL_TTL_HOURS`` (default 24, 1 to 720)."""
    raw = (os.environ if environ is None else environ).get("MODEL_GARDEN_APPROVAL_TTL_HOURS", "").strip()
    if not raw:
        return DEFAULT_TTL_HOURS * 3600
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_TTL_HOURS:
        raise ValueError(f"MODEL_GARDEN_APPROVAL_TTL_HOURS must be a whole number of hours from 1 to {MAX_TTL_HOURS}")
    return int(raw) * 3600


def age_seconds(path: pathlib.Path, record: Mapping[str, Any], key: str, *, now: float | None = None) -> int:
    """Seconds since the record's own timestamp (``requestedAt`` or ``decidedAt``), or since its file was written when it has none."""
    stamp = record.get(key)
    if not (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)):
        stamp = path.stat().st_mtime
    return max(0, int((time.time() if now is None else now) - stamp))


def is_expired(path: pathlib.Path, record: Mapping[str, Any], key: str, *, now: float | None = None, ttl: int | None = None) -> bool:
    return age_seconds(path, record, key, now=now) >= (ttl_seconds() if ttl is None else ttl)


def pending(store: pathlib.Path, *, now: float | None = None, ttl: int | None = None, include_expired: bool = False) -> list[dict[str, Any]]:
    """Requests still waiting for a decision (those that have expired are left out unless ``include_expired``, which marks them)."""
    directory = store / "pending"
    if not directory.exists():
        return []
    limit = ttl_seconds() if ttl is None else ttl
    requests = []
    for path in sorted(directory.glob("*.json")):
        request = _load(path)
        request_id = request.get("requestId")
        if not isinstance(request_id, str) or path.stem != request_id or not REQUEST_ID.fullmatch(request_id):
            raise ValueError(f"unsafe pending approval record: {path}")
        expired = is_expired(path, request, "requestedAt", now=now, ttl=limit)
        if expired and not include_expired:
            continue
        # A recorded decision is no longer actionable in the human inbox.
        # lexists also treats a dangling symlink/unsafe path as occupied.
        if os.path.lexists(store / "decisions" / f"{request_id}.json"):
            continue
        requests.append({**request, "expired": True} if expired else request)
    return requests


def sweep(store: pathlib.Path, *, now: float | None = None, ttl: int | None = None) -> list[dict[str, str]]:
    """Remove expired requests and decisions; returns what was removed as ``{"requestId", "kind"}`` plus the ``tool`` name of a request (identifiers only)."""
    limit = ttl_seconds() if ttl is None else ttl
    removed: list[dict[str, str]] = []
    for kind, key in (("pending", "requestedAt"), ("decisions", "decidedAt")):
        directory = store / kind
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            if not REQUEST_ID.fullmatch(path.stem):
                continue
            try:
                record = _load(path)
                if is_expired(path, record, key, now=now, ttl=limit):
                    path.unlink()
                    item = {"requestId": path.stem, "kind": "request" if kind == "pending" else "decision"}
                    tool = record.get("tool")
                    if kind == "pending" and isinstance(tool, str) and TOOL_NAME.fullmatch(tool):
                        item["tool"] = tool  # a Tool name is safe to tell a person; arguments and justification are never copied
                    removed.append(item)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return removed


def decide(store: pathlib.Path, request_id: str, *, approved: bool, approver: str, now: float | None = None,
           ttl: int | None = None) -> dict[str, Any]:
    request_id = _validate_request_id(request_id)
    approver = approver.strip()
    if not approver:
        raise ValueError("approver must be non-empty")
    pending_path = store / "pending" / f"{request_id}.json"
    if not pending_path.is_file():
        raise ValueError(f"no pending exact action exists for {request_id}")
    request = _load(pending_path)
    if request.get("requestId") != request_id:
        raise ValueError("pending request id does not match filename")
    if is_expired(pending_path, request, "requestedAt", now=now, ttl=ttl):
        raise ValueError(f"the approval request {request_id[:12]} has expired; ask for it again")
    decision = {"requestId": request_id, "approved": approved, "approver": approver, "decidedAt": int(time.time() if now is None else now)}
    _atomic_json(store / "decisions" / f"{request_id}.json", decision)
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=pathlib.Path, help="approval store root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list pending exact actions (expired ones are marked)")
    approve = sub.add_parser("approve", help="approve one exact pending action")
    approve.add_argument("request_id")
    approve.add_argument("--approver", required=True)
    reject = sub.add_parser("reject", help="reject one exact pending action")
    reject.add_argument("request_id")
    reject.add_argument("--approver", required=True)
    args = parser.parse_args()

    try:
        if args.command == "list":
            print(json.dumps(pending(args.store, include_expired=True), indent=2, sort_keys=True))
            return 0
        decision = decide(
            args.store,
            args.request_id,
            approved=args.command == "approve",
            approver=args.approver,
        )
        print(json.dumps(decision, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime approval failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
