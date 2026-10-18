"""Tier 0 built-in work items: a ticket list with no Jira, Linear or GitHub bound (#77).

A client with no work-item system still needs somewhere for "a person has to look at this" to land that is more durable than
a chat message. This store backs ``tickets.search``, ``tickets.create`` and ``tickets.update`` on the governed sidecar and the
runtime inbox. It is deliberately small and conservative:

- **Who sees what.** A verified ``member`` (staff) searches every ticket and updates any of them. Anyone else (a customer, a
  public sender, an unattributed call) can search only the tickets they opened themselves and cannot update at all, so a
  public caller can raise a request but never read the team's list or close someone else's item.
- **Idempotent creation.** A ticket's id is derived from its project, title, description and opener, so a retry, a redelivered
  event or a restarted worker returns the same ticket with ``duplicate: true`` instead of a second one.
- **A short, closed lifecycle.** ``open`` → ``in-progress`` / ``waiting`` → ``done`` or ``cancelled``. ``done`` and
  ``cancelled`` are final (a new ticket is opened instead of reopening one), and every change records who made it and when.
- **Bounded and private.** Field sizes and counts are capped, payment card numbers in text are redacted, and the store is one
  JSON document rewritten atomically under an exclusive lock in a 0700 directory. Text is plaintext on the sidecar-only volume,
  like the ledger and the contact log; the ledger records the project, id and status the capability contract names, never the
  title, description or comments. It is sized for a small business, not a work-item system.

Nothing here has had live-deployment proof.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import time
from collections.abc import Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
PROVIDER_ID = "built-in-tickets"
STATUSES = ("open", "in-progress", "waiting", "done", "cancelled")
FINAL = ("done", "cancelled")
MAX_TICKETS = 5000
MAX_COMMENTS = 50
MAX_TITLE = 120
MAX_DESCRIPTION = 2000
MAX_COMMENT = 1000
MAX_QUERY = 100
MAX_RESULTS = 20
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_ID = re.compile(r"^t-[0-9a-f]{12}$")


def _sibling(name: str, filename: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


_REDACTION = _sibling("model_garden_redaction", "redaction.py")


class TicketError(ValueError):
    """Raised for invalid input, an unknown ticket or one the requester may not touch; never carries ticket text."""


def _text(value: Any, label: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise TicketError(f"{label} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise TicketError(f"{label} exceeds {limit} characters")
    return _REDACTION.redact_cards(cleaned)


class TicketStore:
    def __init__(self, root: pathlib.Path, *, tenant: str) -> None:
        if not isinstance(tenant, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", tenant):
            raise TicketError("tickets need a tenant-scoped client slug")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.tickets.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        descriptor = os.open(self.root / f".{self.tenant}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            document = {"schemaVersion": SCHEMA_VERSION, "tickets": {}}
            if self.path.is_file():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise TicketError("the ticket store is unreadable") from exc
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or not isinstance(loaded.get("tickets"), dict):
                    raise TicketError("the ticket store is unreadable")
                document = loaded
            before = json.dumps(document, sort_keys=True)
            yield document
            if json.dumps(document, sort_keys=True) != before:
                temporary = self.path.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(document, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _ref(self, ticket_id: str) -> str:
        return f"tier0:tickets/{self.tenant}/{ticket_id}"

    def _shape(self, ticket: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
        shaped = {
            "ticketId": ticket["id"], "project": ticket["project"], "title": ticket["title"], "status": ticket["status"],
            "createdAt": ticket["createdAt"], "updatedAt": ticket["updatedAt"], "externalObjectRef": self._ref(ticket["id"]),
        }
        if detail:
            shaped["description"] = ticket["description"]
            shaped["comments"] = [{"by": item["by"], "at": item["at"], "text": item["text"], **({"status": item["status"]} if "status" in item else {})}
                                  for item in ticket["comments"]]
        return shaped

    # -- portable Tools -----------------------------------------------------------------

    def create(self, *, project: Any, title: Any, description: Any = None, requested_by: str, agent: str | None = None,
               now: int | None = None) -> dict[str, Any]:
        if not isinstance(project, str) or not _PROJECT.match(project):
            raise TicketError("project must be a short lowercase name such as support or general")
        clean_title = _text(title, "title", MAX_TITLE)
        clean_description = _text(description, "description", MAX_DESCRIPTION, required=False)
        if not isinstance(requested_by, str) or not requested_by.strip():
            raise TicketError("a ticket records who opened it")
        digest = hashlib.sha256(json.dumps([self.tenant, project, clean_title, clean_description, requested_by],
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        ticket_id = f"t-{digest[:12]}"
        stamp = int(time.time()) if now is None else now
        with self._locked() as document:
            tickets = document["tickets"]
            if ticket_id in tickets:
                return {"provider": PROVIDER_ID, "ticketId": ticket_id, "status": tickets[ticket_id]["status"], "duplicate": True,
                        "externalObjectRef": self._ref(ticket_id)}
            if len(tickets) >= MAX_TICKETS:
                raise TicketError(f"the ticket store is full ({MAX_TICKETS}); close or purge old tickets")
            tickets[ticket_id] = {
                "id": ticket_id, "project": project, "title": clean_title, "description": clean_description, "status": "open",
                "openedBy": requested_by, "agent": agent, "createdAt": stamp, "updatedAt": stamp, "comments": [],
            }
        return {"provider": PROVIDER_ID, "ticketId": ticket_id, "status": "open", "duplicate": False, "externalObjectRef": self._ref(ticket_id)}

    def update(self, *, ticket_id: Any, status: Any = None, comment: Any = None, requested_by: str, principal_access: str | None,
               now: int | None = None) -> dict[str, Any]:
        if principal_access != "member":
            raise TicketError("only verified staff may update tickets")
        if not isinstance(ticket_id, str) or not _ID.match(ticket_id):
            raise TicketError("ticketId must be one returned by tickets.create or tickets.search")
        if status is None and comment is None:
            raise TicketError("give a status, a comment or both")
        if status is not None and status not in STATUSES:
            raise TicketError(f"status must be one of {', '.join(STATUSES)}")
        text = _text(comment, "comment", MAX_COMMENT) if comment is not None else None
        stamp = int(time.time()) if now is None else now
        with self._locked() as document:
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise TicketError("no such ticket")
            if ticket["status"] in FINAL:
                raise TicketError(f"the ticket is {ticket['status']}; open a new ticket instead of reopening it")
            if len(ticket["comments"]) >= MAX_COMMENTS:
                raise TicketError(f"a ticket holds at most {MAX_COMMENTS} comments")
            entry: dict[str, Any] = {"by": requested_by, "at": stamp, "text": text or ""}
            if status is not None:
                entry["status"] = status
                ticket["status"] = status
            ticket["comments"].append(entry)
            ticket["updatedAt"] = stamp
            current = ticket["status"]
        return {"provider": PROVIDER_ID, "ticketId": ticket_id, "status": current, "externalObjectRef": self._ref(ticket_id)}

    def search(self, *, query: Any, requested_by: str, principal_access: str | None, limit: int = MAX_RESULTS,
               status: str | None = None) -> dict[str, Any]:
        needle = _text(query, "query", MAX_QUERY).lower()
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
            raise TicketError(f"limit must be an integer from 1 to {MAX_RESULTS}")
        if status is not None and status not in STATUSES:
            raise TicketError(f"status must be one of {', '.join(STATUSES)}")
        with self._locked() as document:
            tickets = list(document["tickets"].values())
        staff = principal_access == "member"
        found = [
            ticket for ticket in tickets
            if (staff or ticket["openedBy"] == requested_by)
            and (status is None or ticket["status"] == status)
            and (needle in ("*", "all") or needle in ticket["title"].lower() or needle in ticket["description"].lower()
                 or needle in ticket["project"] or needle == ticket["id"])
        ]
        found.sort(key=lambda ticket: (ticket["updatedAt"], ticket["id"]), reverse=True)
        return {"provider": PROVIDER_ID, "tickets": [self._shape(ticket, detail=staff) for ticket in found[:limit]]}

    # -- operator and inbox views ---------------------------------------------------------

    def get(self, ticket_id: str) -> dict[str, Any]:
        """One ticket with its text and history, for an operator on the client host."""
        with self._locked() as document:
            ticket = document["tickets"].get(ticket_id)
        if ticket is None:
            raise TicketError("no such ticket")
        return self._shape(ticket, detail=True)

    def open_items(self) -> list[dict[str, Any]]:
        """Tickets still waiting for a person: identifiers, project, status and ages only (no title, description or comments)."""
        with self._locked() as document:
            tickets = list(document["tickets"].values())
        items = [{"ticketId": ticket["id"], "project": ticket["project"], "status": ticket["status"], "createdAt": ticket["createdAt"],
                  "updatedAt": ticket["updatedAt"]} for ticket in tickets if ticket["status"] not in FINAL]
        return sorted(items, key=lambda item: (item["createdAt"], item["ticketId"]))
