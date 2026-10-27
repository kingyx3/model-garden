"""The built-in inbox: one view of everything on this client runtime that is waiting for a person.

A small business with no work-item system still needs one place to see what the coworker cannot
finish alone. This module reads the stores that already hold those items and summarises them without
copying their contents:

- pending approvals (an exact material action waiting for a mapped approver),
- open Tier 4 hand-offs (an action the coworker prepared for a named person, with its deadline),
- open Tier 0 tickets (work items the coworker or a person opened and nobody has finished), as a count and the oldest age,
- CRM filings that blocked and need a person (the worker retries transient failures itself), as a count and the oldest age,
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
import re
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


def _approval_module():
    """The approval store module (``scripts/runtime-approval.py``) when it is deployed beside the platform, else ``None``."""
    name = "model_garden_inbox_approval"
    if name not in sys.modules:
        path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "runtime-approval.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _approvals(root: pathlib.Path, now: int) -> list[dict[str, Any]]:
    approval = _approval_module()
    ttl = approval.ttl_seconds() if approval is not None else None
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
        if approval is not None and approval.is_expired(path, request, "requestedAt", now=now, ttl=ttl):
            continue  # it can no longer be decided; the person who needs it done asks again
        decision_path = pathlib.Path(root) / "decisions" / f"{request_id}.json"
        if decision_path.exists() or decision_path.is_symlink():
            continue  # first decision wins; do not offer an already-decided action again
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


def _tickets(root: pathlib.Path | None, now: int) -> list[dict[str, Any]]:
    if root is None or not pathlib.Path(root).is_dir():
        return []
    tickets = _sibling("model_garden_inbox_tickets", "tickets.py")
    items: list[dict[str, Any]] = []
    for path in sorted(pathlib.Path(root).glob("*.tickets.json")):
        try:
            open_items = tickets.TicketStore(pathlib.Path(root), tenant=path.name[: -len(".tickets.json")]).open_items()
        except (OSError, ValueError):
            continue
        if open_items:
            items.append({
                "kind": "tickets", "count": len(open_items), "oldestWaitingSeconds": max(0, now - min(item["createdAt"] for item in open_items)),
                "next": "python3 scripts/runtime-tickets.py list  (shows ids, projects and status; `show <id>` prints the text)",
            })
    return items


def _crm_filings(root: pathlib.Path | None, now: int) -> list[dict[str, Any]]:
    """CRM filings that blocked and need a person (the worker retries the transient ones itself); identifiers and ages only."""
    if root is None or not pathlib.Path(root).is_dir():
        return []
    sync = _sibling("model_garden_inbox_crm_sync", "crm_sync.py")
    blocked: list[dict[str, Any]] = []
    for path in sorted(pathlib.Path(root).glob("*.crm-sync.json")):
        try:
            blocked.extend(sync.SyncLedger(pathlib.Path(root), tenant=path.name[: -len(".crm-sync.json")]).list(states=("blocked",)))
        except (OSError, ValueError):
            continue
    if not blocked:
        return []
    return [{
        "kind": "crm-filing", "count": len(blocked), "oldestWaitingSeconds": max(0, now - min(entry["createdAt"] for entry in blocked)),
        "next": "python3 scripts/client-operator.py crm-sync list --project-name <env>  (shows why each did not reach the CRM; "
                "`crm-sync retry <key> --by <you>` once the cause is fixed)",
    }]


def _paused(root: pathlib.Path | None) -> list[dict[str, Any]]:
    if root is None:
        return []
    state = _sibling("model_garden_inbox_pause", "pause.py").PauseStore(pathlib.Path(root)).status()
    if not state["paused"]:
        return []
    return [{"kind": "paused", "by": state.get("by"), "at": state.get("at"), "reason": state.get("reason") or "", "cause": state.get("cause"),
             "next": "an administrator resumes with `resume coworker` in a direct chat, or `client-operator.py resume`"}]


def _unanswered(root: pathlib.Path | None, now: int) -> list[dict[str, Any]]:
    """Questions no approved knowledge answered (#237), as a count of groups and questions and the oldest age; never the text."""
    if root is None or not pathlib.Path(root).is_dir():
        return []
    store_module = _sibling("model_garden_inbox_unanswered", "unanswered.py")
    groups = questions = oldest = 0
    for path in sorted(pathlib.Path(root).glob("*.unanswered.json")):
        try:
            summary = store_module.UnansweredStore(pathlib.Path(root), tenant=path.name[: -len(".unanswered.json")]).open_summary(now)
        except (OSError, ValueError):
            continue
        groups, questions, oldest = groups + summary["groups"], questions + summary["questions"], max(oldest, summary["oldestSeconds"])
    if not groups:
        return []
    return [{"kind": "unanswered", "count": groups, "questions": questions, "oldestWaitingSeconds": oldest,
             "next": "python3 scripts/client-operator.py unanswered list  (an owner reviews the questions and adds an approved knowledge source)"}]


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


def _worker_notices(root: pathlib.Path | None) -> list[dict[str, Any]]:
    """Show posted host-local owner notices, never their text or customer content."""
    if root is None:
        return []
    path = pathlib.Path(root) / "worker-notices.json"
    if path.is_symlink():
        raise ValueError("unsafe worker notice index")
    if not path.exists():
        return []
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("pending"), dict):
        raise ValueError("invalid worker notice index")
    found = []
    for key, notice in state["pending"].items():
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{24}", key) is None or not isinstance(notice, dict):
            raise ValueError("invalid worker notice entry")
        if notice.get("inboxPosted") is not True:
            continue
        to, kind = notice.get("to"), notice.get("kind")
        if not isinstance(to, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}", to) is None:
            raise ValueError("invalid worker notice recipient")
        if not isinstance(kind, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", kind) is None:
            raise ValueError("invalid worker notice kind")
        found.append({
            "kind": "owner-notice", "noticeKind": kind, "to": to, "key": key,
            "next": f"Host operator: python3 scripts/runtime-inbox.py --ack-notice {key} after reviewing the underlying item",
        })
    return sorted(found, key=lambda item: (item["to"], item["key"]))


def collect(
    *,
    approvals: pathlib.Path,
    handoffs: pathlib.Path,
    contacts: pathlib.Path | None = None,
    governance: pathlib.Path | None = None,
    tickets: pathlib.Path | None = None,
    crm_sync: pathlib.Path | None = None,
    unanswered: pathlib.Path | None = None,
    notices: pathlib.Path | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Everything waiting for a person, most urgent first.

    Order: failed or revoked connections, overdue hand-offs, approvals, other hand-offs, degraded
    connections, open tickets, CRM filings that need a person, then records to review.
    """
    current = int(time.time()) if now is None else now
    open_handoffs = sorted(_handoffs(handoffs, current), key=lambda item: (not item["overdue"], item.get("deadline") or 0, item["id"]))
    waiting = sorted(_approvals(approvals, current), key=lambda item: (-item["waitingSeconds"], item["id"]))
    overdue = [item for item in open_handoffs if item["overdue"]]
    due = [item for item in open_handoffs if not item["overdue"]]
    reviews = _review(contacts)
    open_tickets = _tickets(tickets, current)
    filings = _crm_filings(crm_sync, current)
    questions = _unanswered(unanswered, current)
    owner_notices = _worker_notices(notices)
    connections = _connections(governance)
    paused = _paused(governance)
    down = [item for item in connections if item["health"] in ("failed", "revoked")]
    degraded = [item for item in connections if item["health"] == "degraded"]
    return {
        "generatedAt": current,
        "items": [*paused, *down, *overdue, *waiting, *due, *owner_notices, *degraded, *open_tickets, *filings, *questions, *reviews],
        "counts": {
            "paused": len(paused),
            "unanswered": sum(item["count"] for item in questions),
            "approvals": len(waiting),
            "handoffs": len(open_handoffs),
            "overdueHandoffs": len(overdue),
            "reviews": sum(item["count"] for item in reviews),
            "tickets": sum(item["count"] for item in open_tickets),
            "crmFilings": sum(item["count"] for item in filings),
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
        f" {counts['connections']} connection(s) not healthy, {counts['tickets']} open ticket(s),"
        f" {counts['crmFilings']} CRM filing(s) blocked",
    ]
    for item in inbox["items"]:
        if item["kind"] == "paused":
            lines.append(f"  PAUSED    by {item.get('by') or 'unknown'}" + (f": {item['reason']}" if item.get("reason") else "")
                         + " - no new action or scheduled work runs")
        elif item["kind"] == "approval":
            lines.append(f"  APPROVAL  {item['tool'] or 'unknown tool'}  waiting {_age(item['waitingSeconds'])}  id {item['id']}")
        elif item["kind"] == "connection":
            why = f" ({item['reason']})" if item.get("reason") else ""
            lines.append(f"  CONNECTION  {item['family']} is {item['health'].upper()}{why}")
        elif item["kind"] == "unanswered":
            lines.append(f"  UNANSWERED  {item['count']} question group(s) ({item['questions']} ask(s)), oldest waiting {_age(item['oldestWaitingSeconds'])}")
        elif item["kind"] == "tickets":
            lines.append(f"  TICKETS   {item['count']} open, oldest waiting {_age(item['oldestWaitingSeconds'])}")
        elif item["kind"] == "crm-filing":
            lines.append(f"  CRM       {item['count']} filing(s) blocked, oldest waiting {_age(item['oldestWaitingSeconds'])}")
        elif item["kind"] == "owner-notice":
            lines.append(f"  NOTICE    {item['noticeKind']} for {item['to']}  id {item['key']}")
        elif item["kind"] == "handoff":
            due = "OVERDUE" if item["overdue"] else "open"
            lines.append(f"  HAND-OFF  {item['tool']}  for {item['assignee']}  {due} ({item['reason']})  id {item['id']}")
        else:
            lines.append(f"  REVIEW    {item['count']} {item['what'].replace('-', ' ')}")
        lines.append(f"            next: {item['next']}")
    return "\n".join(lines) + "\n"


def for_person(inbox: dict[str, Any], identity: str, *, everything: bool) -> dict[str, Any]:
    """The part of an inbox one person may see in chat.

    An approver or admin (``everything``) sees all of it. Anyone else sees only the hand-offs assigned to them: approvals, tickets,
    connection health and records to review are for the people who run the runtime.
    """
    if everything:
        return inbox
    mine = [item for item in inbox["items"] if
            (item["kind"] == "handoff" and item.get("assignee") == identity) or
            (item["kind"] == "owner-notice" and item.get("to") == identity)]
    return {
        "generatedAt": inbox["generatedAt"], "items": mine,
        "counts": {"approvals": 0, "handoffs": len(mine), "overdueHandoffs": sum(1 for item in mine if item["overdue"]), "reviews": 0, "paused": 0, "unanswered": 0,
                   "tickets": 0, "crmFilings": 0, "connections": 0},
    }


def format_chat(inbox: dict[str, Any]) -> str:
    """The inbox as a short chat message: the same non-sensitive facts, with replies and buttons instead of shell commands."""
    if not inbox["items"]:
        return "Nothing is waiting for you."
    counts = inbox["counts"]
    parts = [f"{counts[key]} {label}" for key, label in (("approvals", "approval(s)"), ("handoffs", "hand-off(s)"), ("tickets", "open ticket(s)"),
                                                          ("crmFilings", "CRM filing(s) blocked"), ("unanswered", "unanswered question group(s)"), ("reviews", "record(s) to review"), ("connections", "connection(s) not healthy")) if counts[key]]
    if counts["overdueHandoffs"]:
        parts.append(f"{counts['overdueHandoffs']} overdue")
    local_notices = sum(item["kind"] == "owner-notice" for item in inbox["items"])
    if local_notices:
        parts.append(f"{local_notices} local notice(s)")
    lines = ["Waiting: " + ", ".join(parts) + "."]
    for item in inbox["items"]:
        if item["kind"] == "paused":
            lines.append("- I am PAUSED" + (f" by {item['by']}" if item.get("by") else "") + (f" ({item['reason']})" if item.get("reason") else "")
                         + ". Reply `resume coworker` to restart me.")
        elif item["kind"] == "approval":
            lines.append(f"- Approval: {item['tool'] or 'an action'}, waiting {_age(item['waitingSeconds'])} (ref {item['id'][:12]}). "
                         "Use the Approve or Reject buttons on its approval message, or review the exact action in the private operator view "
                         f"then reply `approve {item['id']}` or `reject {item['id']}`. A decision is not execution.")
        elif item["kind"] == "handoff":
            ref = item["id"][:12]
            state = "OVERDUE" if item["overdue"] else "open"
            lines.append(f"- Hand-off for {item['assignee']}: {item['tool']}, {state} ({item['reason']}). "
                         f"Reply `done {ref}` when it is done or `declined {ref} <reason>`.")
        elif item["kind"] == "connection":
            why = f" ({item['reason']})" if item.get("reason") else ""
            lines.append(f"- Connection {item['family']} is {item['health'].upper()}{why}. Ask its owner to reconnect it.")
        elif item["kind"] == "unanswered":
            lines.append(f"- {item['count']} customer question(s) had no approved answer, the oldest waiting {_age(item['oldestWaitingSeconds'])}. "
                         "An operator can review them on the client host.")
        elif item["kind"] == "tickets":
            lines.append(f"- {item['count']} open ticket(s), the oldest waiting {_age(item['oldestWaitingSeconds'])}. Ask the coworker to search tickets.")
        elif item["kind"] == "crm-filing":
            lines.append(f"- {item['count']} customer email(s) did not reach the CRM and need a person, the oldest waiting "
                         f"{_age(item['oldestWaitingSeconds'])}. An operator can see why on the client host (`crm-sync list`).")
        elif item["kind"] == "owner-notice":
            lines.append(f"- Local notice {item['noticeKind']} for {item['to']} (ref {item['key']}). "
                         "A host operator can review and acknowledge it in the runtime inbox; this is not owner consent.")
        else:
            lines.append(f"- {item['count']} {item['what'].replace('-', ' ')} to review. An operator can show them on the client host.")
    return "\n".join(lines)
