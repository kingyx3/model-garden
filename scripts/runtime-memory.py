#!/usr/bin/env python3
"""Inspect, export, delete and purge the coworker's remembered facts (run inside governed-tools).

  runtime-memory.py /opt/data/.modelgarden/memory list --scope client --scope agent:receptionist
  runtime-memory.py /opt/data/.modelgarden/memory export --by jordan@client.example
  runtime-memory.py /opt/data/.modelgarden/memory delete m-0123456789ab --by jordan@client.example
  runtime-memory.py /opt/data/.modelgarden/memory purge --by jordan@client.example [--scope conversation:abc]

`list` prints what was remembered (client data, so run it on the client host and agree who may). Every write,
delete, export and purge is a chained ledger event naming the actor, with identifiers and a digest of the text,
never the text. The identity is as asserted on the command line, like approvals; the platform does not verify it.
Records are for one tenant: pass --tenant or it is read from the compiled desired state (`source.client`).
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


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GOVERN = _load("runtime_memory_govern", ROOT / "scripts" / "govern-action.py")
MEMORY = _load("runtime_memory_store", ROOT / "platform" / "core" / "memory.py")


TENANT = _load("runtime_memory_tenant", ROOT / "platform" / "core" / "tenant_identity.py")


def default_tenant(desired_state: pathlib.Path) -> str:
    return TENANT.tenant_of(desired_state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="memory store root")
    parser.add_argument("--tenant", help="client-environment slug (default: from the compiled desired state)")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/../audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="print remembered entries in the named scopes")
    listing.add_argument("--scope", action="append", required=True, help="client, agent:<name> or conversation:<id>; repeatable")
    export = sub.add_parser("export", help="print everything remembered for this client")
    export.add_argument("--by", required=True, help="the named administrator")
    delete = sub.add_parser("delete", help="delete one entry as an administrator")
    delete.add_argument("memory_id")
    delete.add_argument("--by", required=True, help="the named administrator")
    purge = sub.add_parser("purge", help="delete every entry, or one scope's entries")
    purge.add_argument("--by", required=True, help="the named administrator")
    purge.add_argument("--scope")
    args = parser.parse_args(argv)
    audit_path = args.audit_path or args.store.parent / "audit.jsonl"
    try:
        store = MEMORY.MemoryStore(
            args.store, tenant=(args.tenant if args.tenant is not None else default_tenant(DEFAULT_DESIRED_STATE)),
            audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
        )
        if args.command == "list":
            result: object = store.list(args.scope)
        elif args.command == "export":
            result = store.export(requested_by=args.by)
        elif args.command == "delete":
            result = store.delete(args.memory_id, requested_by=args.by, admin=True)
        else:
            result = store.purge(requested_by=args.by, scope=args.scope)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime memory failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
