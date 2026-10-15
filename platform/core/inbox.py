"""The built-in inbox: one view of everything on this client runtime that is waiting for a person.

A small business with no work-item system still needs one place to see what the coworker cannot
finish alone. This module reads the stores that already hold those items and summarises them without
copying their contents:

- pending approvals (an exact material action waiting for a mapped approver),
- open Tier 4 hand-offs (an action the coworker prepared for a named person, with its deadline),
- records that need a person's review in the Tier 0 contact log (unmatched interactions and possible
  duplicates), as counts only,
- connections that are failed, revoked or degraded (a token that expired or was withdrawn is the commonest
  reason a coworker silently stops being able to do something), by capability family and recorded reason.

The summary is deliberately non-sensitive: it names the Tool, the reason, the assignee, the deadline
and the record ID, plus the command that shows the exact content. It never includes action arguments,
interaction summaries, contact fields or message text, so it is safe to show to anyone allowed to
administer the runtime and to paste into a chat. It is a read-only view over existing stores, not a
`tickets.*` provider.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
from typing import Any

REQUEST_ID_LENGTH = 64


def _sibling(name: str, filename: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _approvals(root: pathlib.Path, now: int) -> list[dict[str, Any]]:
    folder = pathlib.Path(root) / "pending"
    items: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        request_id = request.get("requestId") if isinstance(request, dict) else None
        if not isinstance(request_id, str) or path.stem != request_id or len(request_id) != REQUEST_ID_LENGTH:
            continue
        tool = request.get("tool")
        waiting = max(0, now - int(path.stat().st_mtime))
        items.append({
            "kind": "approval", "id": request_id, "tool": tool if isinstance(tool, str) else None,
            "waitingSeconds": waiting,
            "next": f"python3 scripts/client-operator.py approve {request_id} --approver <you>  "
                    "(`approvals` shows the exact action)",
        })
    return items


def _handoffs(root: pathlib.Path, now: int) -> list[dict[str, Any]]:
    handoff = _sibling("model_garden_inbox_handoff", "handoff.py")
    items: list[dict[str, Any]] = []
    try:
        pending = handoff.pending(root)
    except (OSError, ValueError):
        return items
    for document in pending:
        deadline = document.get("deadline")
        items.append({
            "kind": "handoff", "id": document["id"], "tool": document.get("tool"),
            "reason": document.get("reason"), "assignee": document.get("assignee"),
            "deadline": deadline,
            "overdue": isinstance(deadline, int) and now > deadline,
            "next": f"python3 scripts/client-operator.py handoff-done {document['id']} --by <you>  "
                    "(`handoffs` shows the exact action)",
        })
    return items


def _review(root: pathlib.Path | None) -> list[dict[str, Any]]:
    if root is None or not (pathlib.Path(root) / "contacts.json").is_file():
        return []
    contacts = _sibling("model_garden_inbox_contacts", "contacts.py")
    try:
        queue = contacts.ContactStore(pathlib.Path(root)).review_queue()
    except (OSError, ValueError):
        return []
    items = []
    if queue["unmatchedInteractions"]:
        items.append({
            "kind": "review", "what": "unmatched-interactions", "count": len(queue["unmatchedInteractions"]),
            "next": "python3 scripts/client-operator.py contacts review  (prints personal data; run on the client host)",
        })
    if queue["possibleDuplicates"]:
        items.append({
            "kind": "review", "what": "possible-duplicate-contacts", "count": len(queue["possibleDuplicates"]),
            "next": "python3 scripts/client-operator.py contacts review  (prints personal data; run on the client host)",
        })
    return items


def _connections(root: pathlib.Path | None) -> list[dict[str, Any]]:
    if root is None:
        return []
    capabilities = _sibling("model_garden_inbox_capabilities", "capabilities.py")
    items = []
    try:
        records = capabilities.read_connection_health(pathlib.Path(root))
    except (OSError, ValueError):
        return items
    for family, record in sorted(records.items()):
        health = record.get("health")
        if health not in ("failed", "revoked", "degraded"):
            continue
        item: dict[str, Any] = {
            "kind": "connection", "family": family, "health": health,
            "since": record.get("updatedAt"), "recordedBy": record.get("updatedBy"),
            "next": (
                f"ask the connection's owner to reauthorize {family}, then run python3 scripts/client-operator.py "
                f"connection {family} healthy --project-name <env> --actor <you>"
                if health in ("failed", "revoked")
                else f"watch {family}: it is degraded; python3 scripts/client-operator.py connections --project-name <env> shows the detail"
            ),
        }
        if isinstance(record.get("reason"), str):
            item["reason"] = record["reason"]
        items.append(item)
    return items


def collect(
    *,
    approvals: pathlib.Path,
    handoffs: pathlib.Path,
    contacts: pathlib.Path | None = None,
    governance: pathlib.Path | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Everything waiting for a person, most urgent first.

    Order: failed or revoked connections, overdue hand-offs, approvals, other hand-offs, degraded
    connections, then records to review.
    """
    current = int(time.time()) if now is None else now
    open_handoffs = sorted(_handoffs(handoffs, current), key=lambda item: (not item["overdue"], item.get("deadline") or 0, item["id"]))
    waiting = sorted(_approvals(approvals, current), key=lambda item: (-item["waitingSeconds"], item["id"]))
    overdue = [item for item in open_handoffs if item["overdue"]]
    due = [item for item in open_handoffs if not item["overdue"]]
    reviews = _review(contacts)
    connections = _connections(governance)
    down = [item for item in connections if item["health"] in ("failed", "revoked")]
    degraded = [item for item in connections if item["health"] == "degraded"]
    return {
        "generatedAt": current,
        "items": [*down, *overdue, *waiting, *due, *degraded, *reviews],
        "counts": {
            "approvals": len(waiting),
            "handoffs": len(open_handoffs),
            "overdueHandoffs": len(overdue),
            "reviews": sum(item["count"] for item in reviews),
            "connections": len(connections),
        },
    }


def _age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def format_text(inbox: dict[str, Any]) -> str:
    counts = inbox["counts"]
    if not inbox["items"]:
        return "Nothing is waiting for a person.\n"
    lines = [
        f"Waiting for a person: {counts['approvals']} approval(s), {counts['handoffs']} hand-off(s)"
        f" ({counts['overdueHandoffs']} overdue), {counts['reviews']} record(s) to review,"
        f" {counts['connections']} connection(s) not healthy",
    ]
    for item in inbox["items"]:
        if item["kind"] == "approval":
            lines.append(f"  APPROVAL  {item['tool'] or 'unknown tool'}  waiting {_age(item['waitingSeconds'])}  id {item['id']}")
        elif item["kind"] == "connection":
            why = f" ({item['reason']})" if item.get("reason") else ""
            lines.append(f"  CONNECTION  {item['family']} is {item['health'].upper()}{why}")
        elif item["kind"] == "handoff":
            due = "OVERDUE" if item["overdue"] else "open"
            lines.append(f"  HAND-OFF  {item['tool']}  for {item['assignee']}  {due} ({item['reason']})  id {item['id']}")
        else:
            lines.append(f"  REVIEW    {item['count']} {item['what'].replace('-', ' ')}")
        lines.append(f"            next: {item['next']}")
    return "\n".join(lines) + "\n"
