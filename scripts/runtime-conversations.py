#!/usr/bin/env python3
"""Inspect and manage the cross-channel conversation timeline (run inside governed-tools).

  runtime-conversations.py ROOT stats
  runtime-conversations.py ROOT review
  runtime-conversations.py ROOT timeline --conversation cv-0123456789abcdef0123
  runtime-conversations.py ROOT timeline --customer c-1
  runtime-conversations.py ROOT confirm rv-0123456789abcdef --customer c-1 --by jordan@client.example
  runtime-conversations.py ROOT dismiss rv-0123456789abcdef --by jordan@client.example
  runtime-conversations.py ROOT erase --customer c-1 --by jordan@client.example
  runtime-conversations.py ROOT purge --older-than-days 365 --by jordan@client.example

The timeline holds references, labels and digests only (a transcript or message reference, an outcome label, a ledger request id),
never a message, a number or an address, and this command prints nothing more. A conversation links to a customer only on verified
evidence; the review queue holds the ones a person has to decide, and `confirm` records who decided. Every change is ledgered
under the named person. Reading is not ledgered.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONVERSATIONS = _load("runtime_conversations_store", ROOT / "platform" / "core" / "conversations.py")
GOVERN = _load("runtime_conversations_govern", ROOT / "scripts" / "govern-action.py")


def default_tenant(desired_state: pathlib.Path) -> str:
    """The client-environment slug the worker and the other runtime commands use for this deployment."""
    try:
        client = json.loads(desired_state.read_text(encoding="utf-8")).get("source", {}).get("client")
    except (OSError, ValueError, AttributeError):
        client = None
    return re.sub(r"[^a-z0-9-]+", "-", str(client or "").lower()).strip("-")[:63] or "default"


def main(argv: list[str] | None = None, *, now: int | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=pathlib.Path, help="the conversation store directory")
    parser.add_argument("--tenant", help="the client-environment slug (default: the compiled desired state's client)")
    parser.add_argument("--desired-state", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--audit-path", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stats")
    commands.add_parser("review")
    timeline = commands.add_parser("timeline")
    group = timeline.add_mutually_exclusive_group(required=True)
    group.add_argument("--conversation")
    group.add_argument("--customer")
    confirm = commands.add_parser("confirm")
    confirm.add_argument("review_id")
    confirm.add_argument("--customer", required=True)
    confirm.add_argument("--by", required=True)
    dismiss = commands.add_parser("dismiss")
    dismiss.add_argument("review_id")
    dismiss.add_argument("--by", required=True)
    erase = commands.add_parser("erase")
    target = erase.add_mutually_exclusive_group(required=True)
    target.add_argument("--customer")
    target.add_argument("--conversation")
    erase.add_argument("--by", required=True)
    purge = commands.add_parser("purge")
    purge.add_argument("--older-than-days", type=int, required=True)
    purge.add_argument("--by", required=True)
    args = parser.parse_args(argv)
    clock = int(time.time()) if now is None else now
    try:
        tenant = args.tenant or default_tenant(args.desired_state)
        store = CONVERSATIONS.ConversationStore(
            args.root, tenant=tenant,
            audit=lambda action, by, details: GOVERN.record_operator_action(args.audit_path, action, by, details),
        )
        if args.command == "stats":
            result = store.stats()
        elif args.command == "review":
            result = [{key: item[key] for key in ("id", "conversationId", "reason", "candidates", "createdAt")} for item in store.review_queue()]
        elif args.command == "timeline":
            result = store.conversation_timeline(args.conversation) if args.conversation else store.customer_timeline(args.customer)
            result = [{key: item[key] for key in ("at", "kind", "ref", "speaker", "label", "requestId", "conversationId", "channel") if key in item}
                      for item in result]
        elif args.command == "confirm":
            result = store.confirm(args.review_id, args.customer, by=args.by, now=clock)
        elif args.command == "dismiss":
            store.dismiss(args.review_id, by=args.by)
            result = {"dismissed": args.review_id}
        elif args.command == "erase":
            result = (store.erase_customer(args.customer, by=args.by) if args.customer
                      else store.erase_conversation(args.conversation, by=args.by))
        else:
            if not 1 <= args.older_than_days <= 3650:
                raise CONVERSATIONS.ConversationError("--older-than-days is between 1 and 3650")
            result = store.purge_before(clock - args.older_than_days * 86400, by=args.by)
    except (OSError, ValueError) as exc:
        print(f"Runtime conversations failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
