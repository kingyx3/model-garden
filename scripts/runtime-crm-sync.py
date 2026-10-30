#!/usr/bin/env python3
"""See which customer emails reached the CRM and repair the ones that did not (run inside governed-tools).

  runtime-crm-sync.py ROOT list [--json]
  runtime-crm-sync.py ROOT retry KEY --by jordan@client.example

ROOT is the worker's ``crm-sync`` directory. ``list`` shows, for every interaction the worker tried to file, its state (``synced``,
``reconciled``, ``failed`` and retried automatically, ``blocked`` and waiting for a person), attempts, the CRM's reference when it
acknowledged one, and what to do next. It holds identifiers and codes only: no message text, address or number. ``retry`` makes a
blocked or failed interaction pending again under the named person (ledgered); the worker's next ``crm-sync`` pass files it with the
arguments it kept, or says it cannot when none were kept. Nothing here talks to a CRM.
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


SYNC = _load("runtime_crm_sync_ledger", ROOT / "platform" / "core" / "crm_sync.py")
FILING = _load("runtime_crm_sync_filing", ROOT / "platform" / "core" / "crm_filing.py")
GOVERN = _load("runtime_crm_sync_govern", ROOT / "scripts" / "govern-action.py")


TENANT = _load("runtime_crm_sync_tenant", ROOT / "platform" / "core" / "tenant_identity.py")


def default_tenant(desired_state: pathlib.Path) -> str:
    return TENANT.tenant_of(desired_state)


def rows(ledger, payloads) -> list[dict]:
    shown = []
    for entry in ledger.list():
        view = SYNC.describe(entry)
        shown.append({
            "key": entry["key"], "channel": entry["channel"], "interactionType": entry["interactionType"], "state": entry["state"],
            "attempts": entry["attempts"], "lastError": entry.get("lastError"), "providerRef": entry.get("providerRef"),
            "createdAt": entry["createdAt"], "repair": view["repair"],
            "kept": payloads.get(entry["key"]) is not None if entry["state"] not in ("synced", "reconciled") else False,
        })
    return shown


def main(argv: list[str] | None = None, *, now: int | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=pathlib.Path, help="the worker's crm-sync directory")
    parser.add_argument("--tenant", help="the client-environment slug (default: the compiled desired state's client)")
    parser.add_argument("--desired-state", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--audit-path", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    retry = commands.add_parser("retry")
    retry.add_argument("key")
    retry.add_argument("--by", required=True)
    args = parser.parse_args(argv)
    stamp = int(time.time()) if now is None else now
    try:
        tenant = (args.tenant if args.tenant is not None else default_tenant(args.desired_state))
        ledger = SYNC.SyncLedger(
            args.root, tenant=tenant, audit=lambda action, by, details: GOVERN.record_operator_action(args.audit_path, action, by, details),
        )
        payloads = FILING.PayloadStore(args.root / "payloads")
        if args.command == "list":
            shown = rows(ledger, payloads)
            if args.json:
                print(json.dumps({"entries": shown, "counts": SYNC.summary(ledger.list())}, sort_keys=True))
            else:
                counts = SYNC.summary(ledger.list())
                print("CRM filing: " + ", ".join(f"{count} {state}" for state, count in counts.items() if count) if any(counts.values()) else "CRM filing: nothing yet.")
                for row in shown:
                    if row["state"] in ("synced", "reconciled"):
                        continue
                    print(f"- {row['key'][:12]} {row['interactionType']} {row['state']} (attempts {row['attempts']}"
                          f"{', ' + row['lastError'] if row['lastError'] else ''}): {row['repair']}"
                          + ("" if row["kept"] else " The details were not kept, so it cannot be retried automatically."))
            return 0
        key = args.key
        if not re.fullmatch(r"[0-9a-f]{64}", key):  # accept the shown 12-character prefix when it is unambiguous
            matches = [entry["key"] for entry in ledger.list() if entry["key"].startswith(key)] if re.fullmatch(r"[0-9a-f]{8,63}", key) else []
            if len(matches) != 1:
                raise ValueError("give the 64-character key, or the shown prefix of at least 8 characters that matches exactly one entry")
            key = matches[0]
        entry = ledger.retry(key, by=args.by.strip(), now=stamp)
        print(json.dumps({"key": entry["key"], "state": entry["state"]}, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime crm-sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
