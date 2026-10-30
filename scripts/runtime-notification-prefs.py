#!/usr/bin/env python3
"""Record, show or delete a customer's booking-notice consent and opt-out (run inside governed-tools).

  runtime-notification-prefs.py ROOT record --channel email --address a@b.example --purpose confirmation --state granted \\
      --verified-via recipient-reply --by jordan@client.example
  runtime-notification-prefs.py ROOT record --channel whatsapp --address "+65 6123 4567" --purpose reminder --state opted-out --by jordan@client.example
  runtime-notification-prefs.py ROOT status --channel email --address a@b.example
  runtime-notification-prefs.py ROOT forget --channel email --address a@b.example --by jordan@client.example
  runtime-notification-prefs.py ROOT counts

ROOT is the ``notification-prefs`` directory on the governance volume. The address is stored only as a digest; consent (``granted``) needs
evidence that the recipient controls the destination (``--verified-via``), an opt-out needs none and always wins. Changes are ledgered under the
named person (digest, channel, purpose, state; never the address). Nothing here sends a message.
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


PREFS = _load("runtime_notification_prefs_store", ROOT / "platform" / "core" / "notification_prefs.py")
GOVERN = _load("runtime_notification_prefs_govern", ROOT / "scripts" / "govern-action.py")


TENANT = _load("runtime_notification_prefs_tenant", ROOT / "platform" / "core" / "tenant_identity.py")


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

    def recipient(sub):
        sub.add_argument("--channel", required=True, choices=PREFS.CHANNELS)
        sub.add_argument("--address", required=True)

    record = commands.add_parser("record")
    recipient(record)
    record.add_argument("--purpose", required=True, choices=PREFS.PURPOSES)
    record.add_argument("--state", required=True, choices=PREFS.STATES)
    record.add_argument("--verified-via", choices=PREFS.VERIFIED_VIA)
    record.add_argument("--by", required=True)
    status = commands.add_parser("status")
    recipient(status)
    forget = commands.add_parser("forget")
    recipient(forget)
    forget.add_argument("--by", required=True)
    commands.add_parser("counts")
    args = parser.parse_args(argv)
    try:
        store = PREFS.NotificationPrefs(args.root, tenant=(args.tenant if args.tenant is not None else default_tenant(args.desired_state)),
                                        audit=lambda action, by, details: GOVERN.record_operator_action(args.audit_path, action, by, details))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    moment = int(time.time()) if now is None else now
    try:
        if args.command == "record":
            result = store.record(args.channel, args.address, args.purpose, args.state, by=args.by, now=moment, verified_via=args.verified_via)
        elif args.command == "status":
            result = store.status(args.channel, args.address)
        elif args.command == "forget":
            result = store.forget(args.channel, args.address, by=args.by)
        else:
            result = store.counts()
    except PREFS.NotificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
