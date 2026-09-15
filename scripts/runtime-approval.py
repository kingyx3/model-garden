#!/usr/bin/env python3
"""List and decide exact governed runtime actions without a control plane."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")


def _load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _validate_request_id(request_id: str) -> str:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("request id must be a 64-character lowercase SHA-256 hex value")
    return request_id


def pending(store: pathlib.Path) -> list[dict[str, Any]]:
    directory = store / "pending"
    if not directory.exists():
        return []
    requests = []
    for path in sorted(directory.glob("*.json")):
        request = _load(path)
        request_id = request.get("requestId")
        if not isinstance(request_id, str) or path.stem != request_id or not REQUEST_ID.fullmatch(request_id):
            raise ValueError(f"unsafe pending approval record: {path}")
        requests.append(request)
    return requests


def decide(store: pathlib.Path, request_id: str, *, approved: bool, approver: str) -> dict[str, Any]:
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
    decision = {"requestId": request_id, "approved": approved, "approver": approver}
    _atomic_json(store / "decisions" / f"{request_id}.json", decision)
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=pathlib.Path, help="approval store root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list pending exact actions")
    approve = sub.add_parser("approve", help="approve one exact pending action")
    approve.add_argument("request_id")
    approve.add_argument("--approver", required=True)
    reject = sub.add_parser("reject", help="reject one exact pending action")
    reject.add_argument("request_id")
    reject.add_argument("--approver", required=True)
    args = parser.parse_args()

    try:
        if args.command == "list":
            print(json.dumps(pending(args.store), indent=2, sort_keys=True))
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
