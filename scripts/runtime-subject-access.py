#!/usr/bin/env python3
"""Answer a verified customer's data-access or correction request from Model Garden's own Tier 0 stores (run inside governed-tools).

  runtime-subject-access.py ROOT export (--contact c-... | --email a@b.example | --phone "+65 6123 4567") \\
      --verified-by jordan@client.example --method email-link --approved-by sam@client.example --reason "access request by email"
  runtime-subject-access.py ROOT purge
  runtime-subject-access.py ROOT request-correction --contact c-... --field phone --verified-by ... --method call-back --approved-by ... --reason "..."
  runtime-subject-access.py ROOT resolve-correction sa-0123456789ab --status applied --by jordan@client.example
  runtime-subject-access.py ROOT list-corrections [--status open]

ROOT is the governance directory (it holds ``contacts/``, ``conversations/`` and ``subject-access/``). The verifier and the approver must be
different people. The customer must match exactly one contact: no match, several matches, or more than one way of naming them refuse the request.
An export is a 0600 JSON bundle under ``ROOT/subject-access/bundles`` that expires after seven days (``purge`` removes expired ones); it holds the
contact's fields, the interactions filed against them (emails, links, identifiers and numbers in free text masked) and the references of linked
conversations, and states what it leaves out. Corrections are recorded requests; a person changes the source of truth and resolves the request.
The ledger records ids, field names, counts, people and a digest, never a value. Delivering a bundle to the customer is the operator's decision.
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


SUBJECT = _load("runtime_subject_access_store", ROOT / "platform" / "core" / "subject_access.py")
CONTACTS = _load("runtime_subject_access_contacts", ROOT / "platform" / "core" / "contacts.py")
CONVERSATIONS = _load("runtime_subject_access_conversations", ROOT / "platform" / "core" / "conversations.py")
GOVERN = _load("runtime_subject_access_govern", ROOT / "scripts" / "govern-action.py")


def default_tenant(desired_state: pathlib.Path) -> str:
    try:
        client = json.loads(desired_state.read_text(encoding="utf-8")).get("source", {}).get("client")
    except (OSError, ValueError, AttributeError):
        client = None
    return re.sub(r"[^a-z0-9-]+", "-", str(client or "").lower()).strip("-")[:63] or "default"


def main(argv: list[str] | None = None, *, now: int | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=pathlib.Path, help="the governance directory")
    parser.add_argument("--tenant")
    parser.add_argument("--desired-state", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--audit-path", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")))
    commands = parser.add_subparsers(dest="command", required=True)

    def people(sub):
        sub.add_argument("--verified-by", required=True)
        sub.add_argument("--method", required=True, choices=SUBJECT.METHODS)
        sub.add_argument("--approved-by", required=True)
        sub.add_argument("--reason", required=True)

    export = commands.add_parser("export")
    who = export.add_mutually_exclusive_group(required=True)
    who.add_argument("--contact")
    who.add_argument("--email")
    who.add_argument("--phone")
    people(export)
    commands.add_parser("purge")
    request = commands.add_parser("request-correction")
    request.add_argument("--contact", required=True)
    request.add_argument("--field", required=True, choices=SUBJECT.CORRECTABLE)
    people(request)
    resolving = commands.add_parser("resolve-correction")
    resolving.add_argument("request_id")
    resolving.add_argument("--status", required=True, choices=("applied", "declined"))
    resolving.add_argument("--by", required=True)
    listing = commands.add_parser("list-corrections")
    listing.add_argument("--status", choices=SUBJECT.STATUSES)
    args = parser.parse_args(argv)

    tenant = args.tenant or default_tenant(args.desired_state)
    moment = int(time.time()) if now is None else now
    audit = lambda action, by, details: GOVERN.record_operator_action(args.audit_path, action, by, details)  # noqa: E731
    service = SUBJECT.SubjectAccess(
        args.root / "subject-access", tenant=tenant, contacts=CONTACTS.ContactStore(args.root / "contacts"),
        conversations=CONVERSATIONS.ConversationStore(args.root / "conversations", tenant=tenant), audit=audit)
    bundles = args.root / "subject-access" / "bundles"
    try:
        if args.command == "export":
            result = service.export(bundles, verified_by=args.verified_by, approved_by=args.approved_by, method=args.method, reason=args.reason, now=moment,
                                    contact_id=args.contact, email=args.email, phone=args.phone)
        elif args.command == "purge":
            result = {"purged": service.purge_expired(bundles, now=moment)}
        elif args.command == "request-correction":
            result = service.request_correction(contact_id=args.contact, field=args.field, verified_by=args.verified_by, approved_by=args.approved_by,
                                                method=args.method, reason=args.reason, now=moment)
        elif args.command == "resolve-correction":
            result = service.resolve_correction(args.request_id, args.status, by=args.by, now=moment)
        else:
            result = {"corrections": service.corrections(status=args.status)}
    except (SUBJECT.SubjectAccessError, CONTACTS.ContactError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
