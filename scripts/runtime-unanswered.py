#!/usr/bin/env python3
"""See the customer questions no approved knowledge answered, and close or reopen a group (run inside governed-tools).

  runtime-unanswered.py ROOT list [--status open|dismissed|answered]
  runtime-unanswered.py ROOT dismiss q-0123456789ab --by jordan@client.example
  runtime-unanswered.py ROOT answered q-0123456789ab --by jordan@client.example
  runtime-unanswered.py ROOT reopen q-0123456789ab --by jordan@client.example

ROOT is the ``unanswered`` directory on the governance volume. ``list`` prints each group's id, status, count, channels, the masked
sample (emails, links, numbers and long ids hidden, 160 characters at most) and when it was last seen. Mark a group ``answered`` only
after an approved knowledge source covers it. Status changes are ledgered under the named person (ids and counts only, never the sample).
Reading is not ledgered.
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


UNANSWERED = _load("runtime_unanswered_store", ROOT / "platform" / "core" / "unanswered.py")
GOVERN = _load("runtime_unanswered_govern", ROOT / "scripts" / "govern-action.py")
STATUS_FOR = {"dismiss": "dismissed", "answered": "answered", "reopen": "open"}


TENANT = _load("runtime_unanswered_tenant", ROOT / "platform" / "core" / "tenant_identity.py")


def default_tenant(desired_state: pathlib.Path) -> str:
    return TENANT.tenant_of(desired_state)


def main(argv: list[str] | None = None, *, now: int | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("--tenant")
    parser.add_argument("--desired-state", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--audit-path", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=UNANSWERED.STATUSES)
    for name in STATUS_FOR:
        changing = commands.add_parser(name)
        changing.add_argument("group")
        changing.add_argument("--by", required=True)
    args = parser.parse_args(argv)
    try:
        store = UNANSWERED.UnansweredStore(
            args.root, tenant=(args.tenant if args.tenant is not None else default_tenant(args.desired_state)),
            audit=lambda action, by, details: GOVERN.record_operator_action(args.audit_path, action, by, details))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "list":
            for group in store.list(status=args.status):
                channels = ", ".join(f"{name} {count}" for name, count in sorted(group["channels"].items()))
                print(f"{group['id']}  {group['status']:<9}  x{group['count']}  ({channels})  last {group['latestAt']}  {group['sample']}")
            return 0
        group = store.set_status(args.group, STATUS_FOR[args.command], by=args.by, now=int(time.time()) if now is None else now)
        print(f"{group['id']} is now {group['status']}")
    except UNANSWERED.UnansweredError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
