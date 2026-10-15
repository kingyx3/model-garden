#!/usr/bin/env python3
"""Review and erase records in the Tier 0 contact and interaction log, ledgering who did it.

  runtime-contacts.py /opt/data/.modelgarden/contacts review
  runtime-contacts.py /opt/data/.modelgarden/contacts assign <interaction-id> <contact-id> --by jordan@client.example
  runtime-contacts.py /opt/data/.modelgarden/contacts dismiss <review-id> --by jordan@client.example
  runtime-contacts.py /opt/data/.modelgarden/contacts erase-contact <contact-id> --by jordan@client.example
  runtime-contacts.py /opt/data/.modelgarden/contacts erase-interaction <interaction-id> --by jordan@client.example

`review` prints personal data (contact IDs and interaction summaries) for a person to act on, so run
it only on the client's host. Every change writes a chained `operator-action` event naming the person
and the identifiers (never the record contents). The identity is as asserted on the command line,
like approvals; the platform does not verify it.
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


GOVERN = _load("runtime_contacts_govern", ROOT / "scripts" / "govern-action.py")
CONTACTS = _load("runtime_contacts_store", ROOT / "platform" / "core" / "contacts.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="contact store root")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/../audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("review", help="list possible duplicates and unmatched interactions")
    assign = sub.add_parser("assign", help="attach an unmatched interaction to a contact")
    assign.add_argument("interaction_id")
    assign.add_argument("contact_id")
    dismiss = sub.add_parser("dismiss", help="dismiss one possible-duplicate review item")
    dismiss.add_argument("review_id")
    erase_contact = sub.add_parser("erase-contact", help="erase a contact and its interactions")
    erase_contact.add_argument("contact_id")
    erase_interaction = sub.add_parser("erase-interaction", help="erase one interaction")
    erase_interaction.add_argument("interaction_id")
    for command in (assign, dismiss, erase_contact, erase_interaction):
        command.add_argument("--by", required=True, help="the named person performing the action")
    args = parser.parse_args(argv)
    store = CONTACTS.ContactStore(args.store)
    audit_path = args.audit_path or args.store.parent / "audit.jsonl"
    try:
        if args.command == "review":
            print(json.dumps(store.review_queue(), indent=2, sort_keys=True))
            return 0
        if args.command == "assign":
            result = store.assign_interaction(args.interaction_id, args.contact_id)
        elif args.command == "dismiss":
            result = store.dismiss_review(args.review_id)
        elif args.command == "erase-contact":
            result = store.erase_contact(args.contact_id)
        else:
            result = store.erase_interaction(args.interaction_id)
        GOVERN.record_operator_action(audit_path, f"contacts.{args.command}", args.by, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime contacts failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
