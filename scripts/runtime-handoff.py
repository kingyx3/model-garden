#!/usr/bin/env python3
"""List and close Tier 4 human hand-offs on a governed runtime, ledgering who did what.

  runtime-handoff.py /opt/data/.modelgarden/handoffs list
  runtime-handoff.py /opt/data/.modelgarden/handoffs done <id> --by jordan@client.example
  runtime-handoff.py /opt/data/.modelgarden/handoffs decline <id> --by jordan@client.example --note "not our policy"
  runtime-handoff.py /opt/data/.modelgarden/handoffs due [--escalate-to owner@client.example]
  runtime-handoff.py /opt/data/.modelgarden/handoffs notified <id> assigned|reminder|escalation --via email
  runtime-handoff.py /opt/data/.modelgarden/handoffs deliver --people people.json [--escalate-to owner@client.example]
  runtime-handoff.py /opt/data/.modelgarden/handoffs reply --people people.json --channel slack --conversation D012345 \
      --sender U012345 --direct "done 3fa1c9b2e4d1 booked it"

Closing a hand-off writes a chained `handoff` event (tier 4, provider human) naming the person.
The coworker may report only what this record says: it never claims a person acted before then.

`due` prints the notices that should reach a person now (assigned, one reminder before the deadline, one
escalation once overdue). They carry the Tool, deadline and reference, never the action's arguments.
Delivery is the deployment's channel transport; after the provider accepts a notice, `notified` records
it (and ledgers an operator action naming the channel) so it is not offered again. A notice that was not
recorded is offered again.

`deliver` runs that loop from a people directory (`{"jordan@client.example": {"channel": "inbox"}}`): each due
notice goes to the channel its recipient chose and is recorded only after it is delivered. This command carries
no channel credentials, so it can deliver to `inbox` only; a person whose channel needs a transport (slack,
teams, email) is reported as `no-channel` and stays due for a deployment that registers one
(`platform/core/notice_delivery.py`). Nothing is marked delivered because it was merely visible.

`reply` is the way back (`platform/core/handoff_reply.py`): a channel adapter that has authenticated an inbound message passes its
channel, conversation id and verified sender id here with `--direct` for a one-to-one conversation, and the person's `done <ref>`,
`declined <ref> [note]` or `seen <ref>` closes or acknowledges the hand-off under the directory identity, ledgered as the person and
channel. It prints the JSON result whose `reply` the adapter sends back. Without `--direct` nothing is closed.
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
DELIVERY = _load("runtime_handoff_delivery", ROOT / "platform" / "core" / "notice_delivery.py")
REPLY = _load("runtime_handoff_reply", ROOT / "platform" / "core" / "handoff_reply.py")


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
    deliver = sub.add_parser("deliver", help="deliver the due notices to each person's chosen channel (inbox only without a transport)")
    deliver.add_argument("--people", type=pathlib.Path, required=True, help="JSON object: person -> {channel, address}")
    deliver.add_argument("--escalate-to", help="who an overdue hand-off is escalated to (default: the assignee again)")
    answer = sub.add_parser("reply", help="apply a person's channel reply (done/declined/seen <ref>) from an adapter-authenticated message")
    answer.add_argument("--people", type=pathlib.Path, required=True, help="JSON object: person -> {channel, address, identity?}")
    answer.add_argument("--channel", required=True, help="the channel the verified message arrived on, for example slack")
    answer.add_argument("--conversation", required=True, help="the verified conversation id it arrived in")
    answer.add_argument("--sender", help="the adapter-verified user id of the sender on that channel")
    answer.add_argument("--direct", action="store_true", help="the conversation is one-to-one with the sender (required to close)")
    answer.add_argument("--escalate-to", help="the escalation contact who may also answer once a hand-off is overdue")
    answer.add_argument("text", help="the message text")
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
        if args.command == "deliver":
            directory = DELIVERY.Directory(json.loads(args.people.read_text(encoding="utf-8")))
            results = DELIVERY.deliver(
                [DELIVERY.handoff_source(args.store, escalate_to=args.escalate_to)], directory, {},
                audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
            )
            print(json.dumps(results, indent=2, sort_keys=True))
            return 0
        if args.command == "reply":
            replies = REPLY.HandoffReplies(
                args.store, DELIVERY.Directory(json.loads(args.people.read_text(encoding="utf-8"))),
                complete=lambda handoff_id, outcome, by, **kwargs: GOVERN.complete_handoff(
                    args.store, handoff_id, outcome, by, audit_path=audit_path, **kwargs
                ),
                escalate_to=args.escalate_to,
                audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
            )
            result = replies.handle(args.channel, args.conversation, args.text, direct=args.direct, sender_id=args.sender)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] in ("closed", "acknowledged") else 1
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
