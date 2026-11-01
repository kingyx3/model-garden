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
import email
import email.policy
import email.utils
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
    """Resolve a single exact client slug, never silently route mail into 'default'.

    A missing or corrupt compiled identity is an MTA temporary failure, not
    permission to accept customer messages under a different client's spool.
    """
    try:
        desired = json.loads(desired_state.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("tenant identity unavailable") from exc
    source = desired.get("source") if isinstance(desired, dict) else None
    client = source.get("client") if isinstance(source, dict) else None
    if not isinstance(client, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", client):
        raise ValueError("tenant identity unavailable")
    return client


# Authentication-Results in an RFC 5322 message is untrusted input at a
# generic pipe boundary. Only an independently verified MTA result, supplied
# by the local delivery program rather than copied from the message, can
# establish the sender for the downstream public-principal policy.
_AUTH_SERVER = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")


def prepare_mta_message(raw: bytes, *, verified_sender: str | None = None,
                        authserv_id: str | None = None) -> bytes:
    """Strip untrusted auth headers; optionally add one MTA-attested DMARC result.

    Both optional values must come from a trusted local delivery-agent config
    and its independent authentication result. Neither may be extracted from
    the incoming message's headers, body, SMTP HELO or caller-controlled args.
    """
    if (verified_sender is None) != (authserv_id is None):
        raise ValueError("MTA sender attestation needs both verified sender and authserv id")
    message = email.message_from_bytes(raw, policy=email.policy.SMTP)
    # Delete every occurrence, including an attacker-supplied header that
    # happens to use the receiver's own authserv-id.
    if message.get_all("Authentication-Results"):
        del message["Authentication-Results"]
    if verified_sender is not None:
        trusted = authserv_id.strip().lower()
        address = verified_sender.strip().lower()
        from_headers = message.get_all("From") or []
        parsed = email.utils.getaddresses([str(value) for value in from_headers])
        if (not _AUTH_SERVER.fullmatch(trusted)
                or not SPOOL._ADDRESS.fullmatch(address)
                or len(from_headers) != 1 or len(parsed) != 1
                or parsed[0][1].strip().lower() != address):
            raise ValueError("MTA attestation does not match a single valid sender")
        domain = address.rpartition("@")[2]
        message["Authentication-Results"] = f"{trusted}; dmarc=pass header.from={domain}"
    return message.as_bytes(policy=email.policy.SMTP)


def main(argv: list[str] | None = None, stdin=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="mail spool root")
    parser.add_argument("--address", action="append", required=True, help="an inbound address of this client (repeatable)")
    parser.add_argument("--tenant", help="client-environment slug (default: from the compiled desired state)")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/../audit.jsonl)")
    parser.add_argument("--retention-days", type=int, default=SPOOL.DEFAULT_RETENTION_DAYS)
    parser.add_argument("--verified-sender", help="MTA-verified RFC 5322 From address (trusted local delivery only)")
    parser.add_argument("--mta-authserv-id", help="authserv-id of the MTA that independently verified DMARC")
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
        tenant = args.tenant if args.tenant is not None else tenant_of(DEFAULT_DESIRED_STATE)
    except ValueError:
        print("Runtime mailbox tenant identity unavailable; supply --tenant or repair desired state", file=sys.stderr)
        return TEMPFAIL if args.command == "ingest" else 1
    try:
        spool = SPOOL.MailSpool(
            args.store, tenant=tenant, addresses=args.address,
            retention_days=args.retention_days,
            audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
        )
        if args.command == "ingest":
            try:
                raw = (stdin or sys.stdin.buffer).read(SPOOL.MAX_MESSAGE_BYTES + 1)
                raw = prepare_mta_message(raw, verified_sender=args.verified_sender, authserv_id=args.mta_authserv_id)
                print(json.dumps(spool.ingest(raw), sort_keys=True))
            except Exception as exc:  # noqa: BLE001 - ledger or storage failures must tell the MTA to retry
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
