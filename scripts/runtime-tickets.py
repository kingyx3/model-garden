#!/usr/bin/env python3
"""List, show and update the Tier 0 built-in tickets on a governed runtime (run inside governed-tools or on the client host).

  runtime-tickets.py list [--status open] [--tenant acme]
  runtime-tickets.py show t-0123456789ab [--tenant acme]
  runtime-tickets.py update t-0123456789ab --status in-progress --comment "Calling back at 3pm" --by jordan@client.example

`list` prints ids, projects, titles and status only. `show` prints the description and history, which may hold customer details, so run it
where that is allowed. `update` is a person acting on the runtime: it names who (`--by`, an email) and is ledgered as an operator action with
the ticket id and status, never the comment text. The tickets are `platform/core/tickets.py`; the same list is behind `tickets.search`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PERSON = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tenants(directory: pathlib.Path, wanted: str | None) -> list[str]:
    found = sorted(path.name[: -len(".tickets.json")] for path in directory.glob("*.tickets.json")) if directory.is_dir() else []
    if wanted is not None:
        if wanted not in found:
            raise ValueError(f"no tickets are kept for {wanted!r}")
        return [wanted]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", type=pathlib.Path, default=pathlib.Path(os.environ.get("MODEL_GARDEN_GOVERNANCE_ROOT", "/opt/data/.modelgarden")))
    parser.add_argument("--tenant", help="client slug when the volume holds more than one")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--status")
    show = sub.add_parser("show")
    show.add_argument("ticket_id")
    update = sub.add_parser("update")
    update.add_argument("ticket_id")
    update.add_argument("--status")
    update.add_argument("--comment")
    update.add_argument("--by", required=True, help="the person making the change (an email address)")
    args = parser.parse_args(argv)
    try:
        tickets = _load("runtime_tickets_core", "platform/core/tickets.py")
        directory = args.data_root / "tickets"
        tenants = _tenants(directory, args.tenant)
        if args.command == "list":
            rows = []
            for tenant in tenants:
                store = tickets.TicketStore(directory, tenant=tenant)
                found = store.search(query="*", requested_by="operator", principal_access="member", status=args.status, limit=tickets.MAX_RESULTS)
                rows += [{"tenant": tenant, **{key: item[key] for key in ("ticketId", "project", "title", "status", "updatedAt")}} for item in found["tickets"]]
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        owners = [tenant for tenant in tenants if _has(directory, tenant, args.ticket_id, tickets)]
        if len(owners) != 1:
            raise ValueError("that ticket was not found" if not owners else "that ticket id is in more than one tenant; pass --tenant")
        store = tickets.TicketStore(directory, tenant=owners[0])
        if args.command == "show":
            print(json.dumps(store.get(args.ticket_id), indent=2, sort_keys=True))
            return 0
        if not PERSON.match(args.by):
            raise ValueError("--by must be the person's email address")
        result = store.update(ticket_id=args.ticket_id, status=args.status, comment=args.comment, requested_by=args.by, principal_access="member")
        govern = _load("runtime_tickets_govern", "scripts/govern-action.py")
        govern.record_operator_action(args.data_root / "audit.jsonl", "ticket.update", args.by,
                                      {"tenant": owners[0], "ticketId": args.ticket_id, **({"status": args.status} if args.status else {}),
                                       "commented": args.comment is not None})
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Tickets failed: {exc}", file=sys.stderr)
        return 1


def _has(directory: pathlib.Path, tenant: str, ticket_id: str, tickets) -> bool:
    try:
        tickets.TicketStore(directory, tenant=tenant).get(ticket_id)
        return True
    except tickets.TicketError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
