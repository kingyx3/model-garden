#!/usr/bin/env python3
"""Receive customer email into the client's built-in inbound spool and inspect it (run inside governed-tools).

  runtime-mailbox.py /opt/data/.modelgarden/mail --address support@client.example ingest < message.eml
  runtime-mailbox.py /opt/data/.modelgarden/mail --address support@client.example stats
  runtime-mailbox.py /opt/data/.modelgarden/mail --address support@client.example requeue m-0123456789abcdef01234567 --by jordan@client.example
  runtime-mailbox.py /opt/data/.modelgarden/mail --address support@client.example purge

`ingest` reads one raw message on standard input, the way a mail server's pipe or delivery agent hands mail over. Exit status is for
that server: 0 when the message was stored, was already stored, was dropped because it was not addressed to this client or was
unusable (dropping is deliberate: refusing would bounce a message to whoever forged its sender), and 75 (temporary failure, the
server retries) when the spool is full or cannot be written. Nothing here listens on a network port, sets up DNS or MX, reads
a mailbox on its own or answers anyone; the support mailbox handler drains the spool (`platform/channels/mail_spool.py`). Records and the
ledger hold identifiers, sizes and states only. Pass --tenant or it is read from the compiled desired state (`source.client`).
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
DEFAULT_DESIRED_STATE = pathlib.Path(os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json"))
TEMPFAIL = 75


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GOVERN = _load("runtime_mailbox_govern", ROOT / "scripts" / "govern-action.py")
SPOOL = _load("runtime_mailbox_spool", ROOT / "platform" / "channels" / "mail_spool.py")


def tenant_of(desired_state: pathlib.Path) -> str:
    try:
        client = json.loads(desired_state.read_text(encoding="utf-8")).get("source", {}).get("client")
    except (OSError, json.JSONDecodeError, AttributeError):
        client = None
    return re.sub(r"[^a-z0-9-]+", "-", str(client or "").lower()).strip("-")[:63] or "default"


def main(argv: list[str] | None = None, stdin=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="mail spool root")
    parser.add_argument("--address", action="append", required=True, help="an inbound address of this client (repeatable)")
    parser.add_argument("--tenant", help="client-environment slug (default: from the compiled desired state)")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/../audit.jsonl)")
    parser.add_argument("--retention-days", type=int, default=SPOOL.DEFAULT_RETENTION_DAYS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="store one raw message read from standard input")
    sub.add_parser("stats", help="print counts by state and quarantined ids (no contents)")
    requeue = sub.add_parser("requeue", help="try a quarantined message again")
    requeue.add_argument("message_id")
    requeue.add_argument("--by", required=True, help="the named person deciding it")
    sub.add_parser("purge", help="delete messages past the retention period")
    args = parser.parse_args(argv)
    audit_path = args.audit_path or args.store.parent / "audit.jsonl"
    try:
        spool = SPOOL.MailSpool(
            args.store, tenant=args.tenant or tenant_of(DEFAULT_DESIRED_STATE), addresses=args.address,
            retention_days=args.retention_days,
            audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
        )
        if args.command == "ingest":
            try:
                raw = (stdin or sys.stdin.buffer).read(SPOOL.MAX_MESSAGE_BYTES + 1)
                print(json.dumps(spool.ingest(raw), sort_keys=True))
            except (SPOOL.SpoolFull, OSError) as exc:
                print(f"Runtime mailbox deferred: {type(exc).__name__}", file=sys.stderr)
                return TEMPFAIL
            return 0
        if args.command == "stats":
            print(json.dumps(spool.stats(), indent=2, sort_keys=True))
        elif args.command == "requeue":
            print(json.dumps(spool.requeue(args.message_id, by=args.by), sort_keys=True))
        else:
            print(json.dumps(spool.purge(), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime mailbox failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
