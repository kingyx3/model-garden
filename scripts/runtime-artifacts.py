#!/usr/bin/env python3
"""List, delete and expire the coworker's stored deliverables (run inside governed-tools).

  runtime-artifacts.py /opt/data/.modelgarden/artifacts list [--requested-by jordan@client.example]
  runtime-artifacts.py /opt/data/.modelgarden/artifacts delete a-0123456789ab --by jordan@client.example
  runtime-artifacts.py /opt/data/.modelgarden/artifacts purge-expired

`list` prints names, requesters, sources and expiry, never file contents (client data, so run it on the client host).
Every delete and expiry is a chained ledger event with identifiers and a digest. The actor is as asserted on the
command line, like approvals; the platform does not verify it. Records are for one tenant: pass --tenant or it is
read from the compiled desired state (`source.client`); the retention period is --retention-days (default 365).
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


GOVERN = _load("runtime_artifacts_govern", ROOT / "scripts" / "govern-action.py")
ARTIFACTS = _load("runtime_artifacts_store", ROOT / "platform" / "core" / "artifacts.py")


TENANT = _load("runtime_artifacts_tenant", ROOT / "platform" / "core" / "tenant_identity.py")


def default_tenant(desired_state: pathlib.Path) -> str:
    return TENANT.tenant_of(desired_state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="artifact store root")
    parser.add_argument("--tenant", help="client-environment slug (default: from the compiled desired state)")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/../audit.jsonl)")
    parser.add_argument("--retention-days", type=int, default=ARTIFACTS.DEFAULT_RETENTION_DAYS)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="print stored deliverables (metadata only)")
    listing.add_argument("--requested-by", help="only what this person asked for")
    delete = sub.add_parser("delete", help="delete one deliverable as an administrator")
    delete.add_argument("artifact_id")
    delete.add_argument("--by", required=True, help="the named administrator")
    sub.add_parser("purge-expired", help="delete everything past the retention period")
    args = parser.parse_args(argv)
    audit_path = args.audit_path or args.store.parent / "audit.jsonl"
    try:
        store = ARTIFACTS.ArtifactStore(
            args.store, tenant=(args.tenant if args.tenant is not None else default_tenant(DEFAULT_DESIRED_STATE)), retention_days=args.retention_days,
            audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
        )
        if args.command == "list":
            result: object = store.list(requested_by=args.requested_by)
        elif args.command == "delete":
            result = store.delete(args.artifact_id, requested_by=args.by, admin=True)
        else:
            result = store.purge_expired()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime artifacts failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
