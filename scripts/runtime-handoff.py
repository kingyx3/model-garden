#!/usr/bin/env python3
"""List and close Tier 4 human hand-offs on a governed runtime, ledgering who did what.

  runtime-handoff.py /opt/data/.modelgarden/handoffs list
  runtime-handoff.py /opt/data/.modelgarden/handoffs done <id> --by jordan@client.example
  runtime-handoff.py /opt/data/.modelgarden/handoffs decline <id> --by jordan@client.example --note "not our policy"
  runtime-handoff.py /opt/data/.modelgarden/handoffs due [--escalate-to owner@client.example]
  runtime-handoff.py /opt/data/.modelgarden/handoffs notified <id> assigned|reminder|escalation --via email

Closing a hand-off writes a chained `handoff` event (tier 4, provider human) naming the person.
The coworker may report only what this record says: it never claims a person acted before then.

`due` prints the notices that should reach a person now (assigned, one reminder before the deadline, one
escalation once overdue). They carry the Tool, deadline and reference, never the action's arguments.
Delivery is the deployment's channel transport; after the provider accepts a notice, `notified` records
it (and ledgers an operator action naming the channel) so it is not offered again. A notice that was not
recorded is offered again.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GOVERN = _load("runtime_handoff_govern", ROOT / "scripts" / "govern-action.py")
HANDOFF = GOVERN.HANDOFF


def summary(document: dict) -> dict:
    return {
        "id": document["id"],
        "status": document["status"],
        "tool": document["tool"],
        "reason": document["reason"],
        "assignee": document["assignee"],
        "deadline": document["deadline"],
        "arguments": document["request"]["arguments"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="hand-off store root")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/../audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list pending hand-offs with their exact content")
    due = sub.add_parser("due", help="print the notices that should reach a person now (no arguments, safe for any channel)")
    due.add_argument("--escalate-to", help="who an overdue hand-off is escalated to (default: the assignee again)")
    notified = sub.add_parser("notified", help="record that a notice was delivered")
    notified.add_argument("handoff_id")
    notified.add_argument("kind", choices=HANDOFF.NOTICE_KINDS)
    notified.add_argument("--via", required=True, help="the channel that delivered it, for example email or slack")
    for name in ("done", "decline"):
        command = sub.add_parser(name, help=f"mark one hand-off {name}")
        command.add_argument("handoff_id")
        command.add_argument("--by", required=True, help="the named person closing it")
        command.add_argument("--note")
    args = parser.parse_args(argv)
    audit_path = args.audit_path or args.store.parent / "audit.jsonl"
    try:
        if args.command == "due":
            print(json.dumps(HANDOFF.due_notices(args.store, escalate_to=args.escalate_to), indent=2, sort_keys=True))
            return 0
        if args.command == "notified":
            HANDOFF.mark_notified(args.store, args.handoff_id, args.kind, via=args.via)
            GOVERN.record_operator_action(
                audit_path, "handoff.notified", "runtime:handoff-notifier",
                {"handoffId": args.handoff_id, "kind": args.kind, "via": args.via},
            )
            print(json.dumps({"handoffId": args.handoff_id, "kind": args.kind, "via": args.via}, sort_keys=True))
            return 0
        if args.command == "list":
            print(json.dumps([summary(item) for item in HANDOFF.pending(args.store)], indent=2, sort_keys=True))
            return 0
        document = GOVERN.complete_handoff(
            args.store,
            args.handoff_id,
            "done" if args.command == "done" else "declined",
            args.by,
            audit_path=audit_path,
            note=args.note,
        )
        print(json.dumps(HANDOFF.describe(document), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime hand-off failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
