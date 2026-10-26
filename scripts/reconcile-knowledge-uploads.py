#!/usr/bin/env python3
"""Operator-only, dry-run-first reconciliation of private Tier 0 upload drafts.

Run on the authorized client host against its mounted data volume. This never
promotes KnowledgeSources or deletes imported workspace files and backups.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
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


UPLOADS = _load("model_garden_operator_uploads", ROOT / "platform/core/knowledge_uploads.py")
GOVERN = _load("model_garden_operator_upload_govern", ROOT / "scripts/govern-action.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=pathlib.Path, required=True,
                        help="mounted private .modelgarden data root (not the workspace)")
    parser.add_argument("--tenant", required=True, help="exact client tenant slug")
    parser.add_argument("--actor", required=True, help="operator:<named-person> (host authorization required)")
    parser.add_argument("--retention-days", type=int, required=True,
                        help="reviewed EnvironmentBinding worker.artifact_retention_days (1-3650)")
    parser.add_argument("--audit-path", type=pathlib.Path, help="existing client Action Ledger JSONL")
    parser.add_argument("--erase-id", help="full opaque upload ID for a separately authorized erasure")
    parser.add_argument("--reason", choices=("owner-request", "reconciliation"))
    parser.add_argument("--apply", action="store_true", help="perform audit-gated deletion; default is dry-run")
    parser.add_argument("--confirm-tenant", help="must exactly match --tenant before --apply")
    args = parser.parse_args(argv)
    if args.apply:
        if args.confirm_tenant != args.tenant:
            parser.error("--apply requires --confirm-tenant equal to --tenant")
        if args.audit_path is None or not args.audit_path.is_file() or args.audit_path.is_symlink():
            parser.error("--apply requires an existing regular client --audit-path")
    if args.erase_id and args.reason is None:
        parser.error("--erase-id requires --reason and documented owner/reconciliation authority")
    try:
        result = UPLOADS.purge_expired(
            args.data_root / "knowledge-uploads", tenant=args.tenant,
            now=int(time.time()), retention_days=args.retention_days,
            actor=args.actor, dry_run=not args.apply, erase_id=args.erase_id, reason=args.reason,
            audit=(lambda action, by, details: GOVERN.record_operator_action(
                args.audit_path, action, by, details)) if args.apply else (lambda *unused: None),
        )
    except (UPLOADS.UploadError, OSError) as exc:
        print(f"reconciliation refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"tenant": args.tenant, "dryRun": not args.apply,
                      "count": len(result), "uploadIds": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
