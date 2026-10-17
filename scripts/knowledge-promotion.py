#!/usr/bin/env python3
"""Gate a change to the approved business rules on eval evidence, and roll one back (#116).

  compile-workspace.py <workspace> --agent receptionist --output desired-state.json
  eval-receptionist.py <workspace> --evidence evidence.json
  knowledge-promotion.py check    desired-state.json --record knowledge-promotion.json --evidence evidence.json [--owners a@x,b@x --by a@x]
  knowledge-promotion.py promote  desired-state.json --record knowledge-promotion.json --evidence evidence.json --by a@x [--owners ...]
  knowledge-promotion.py verify   desired-state.json --record knowledge-promotion.json
  knowledge-promotion.py rollback --record knowledge-promotion.json --by a@x
  knowledge-promotion.py plan     --record knowledge-promotion.json

`check` exits 0 when the change may be promoted (no critical change, or valid evidence covers the exact content) and 1 with the
reasons when it may not. `promote` does the same check and rewrites the record (kept in the client workspace repository, so the owner
reviews it like any other change) keeping the previous promoted snapshot for rollback. `verify` exits 1 when the content about to be
deployed differs from the promoted snapshot (a change that skipped promotion). `rollback` makes the previous snapshot the promoted one
and `plan` lists the sources, versions and digests to restore in the workspace; restoring text is a repository action, after which
`verify` proves it. Records and output hold names, versions and short digests, never rule text. The promoter's identity is as asserted
on the command line (restricted to --owners when given); it is not authenticated. Nothing here calls a model or a provider.
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


PROMOTION = _load("knowledge_promotion_cli", ROOT / "platform" / "core" / "knowledge_promotion.py")


def _json(path: pathlib.Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {label} {path}: {type(exc).__name__}") from exc


def _record(path: pathlib.Path | None):
    if path is None or not path.is_file():
        return None
    document = _json(path, "promotion record")
    if not isinstance(document, dict) or document.get("schemaVersion") != PROMOTION.SCHEMA_VERSION:
        raise ValueError("the promotion record has an unsupported format")
    return document


def _write(path: pathlib.Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "promote", "verify"):
        command = sub.add_parser(name)
        command.add_argument("desired_state", type=pathlib.Path, help="compiled desired-state.json for the Agent")
        command.add_argument("--record", type=pathlib.Path, required=True, help="the promotion record (created by the first promote)")
        if name != "verify":
            command.add_argument("--evidence", type=pathlib.Path, help="evidence written by eval-receptionist.py --evidence")
            command.add_argument("--owners", help="comma-separated named owners allowed to promote (governance.owners)")
            command.add_argument("--suite", default=PROMOTION.DEFAULT_SUITE)
            command.add_argument("--max-age-days", type=int, default=PROMOTION.DEFAULT_MAX_AGE_SECONDS // 86400)
            command.add_argument("--by", help="the named person promoting" + (" (required)" if name == "promote" else ""))
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--record", type=pathlib.Path, required=True)
    rollback.add_argument("--by", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--record", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    try:
        record = _record(args.record)
        now = int(time.time())
        if args.command in ("check", "promote", "verify"):
            desired = _json(args.desired_state, "desired state")
        if args.command == "verify":
            result = PROMOTION.verify(desired, record)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["matches"] else 1
        if args.command in ("check", "promote"):
            evidence = _json(args.evidence, "evidence") if args.evidence else None
            owners = [item.strip() for item in args.owners.split(",") if item.strip()] if args.owners else None
            options = dict(owners=owners, suite=args.suite, max_age_seconds=args.max_age_days * 86400)
            if args.command == "check":
                result = PROMOTION.check(desired, record, evidence, now=now, promoted_by=args.by, **options)
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0 if result["allowed"] else 1
            if not args.by:
                parser.error("promote needs --by")
            try:
                updated = PROMOTION.promote(desired, record, evidence, by=args.by, now=now, **options)
            except PROMOTION.PromotionBlocked as exc:
                print(json.dumps({"allowed": False, "reasons": exc.reasons}, indent=2, sort_keys=True))
                return 1
            _write(args.record, updated)
            print(json.dumps({"promoted": True, "sources": sorted(updated["promoted"]["sources"]), "by": args.by}, sort_keys=True))
            return 0
        if args.command == "rollback":
            steps = PROMOTION.rollback_plan(record)  # what to restore in the workspace, from the record as it is now
            _write(args.record, PROMOTION.rollback(record, by=args.by, now=now))
            print(json.dumps({"rolledBack": True, "plan": steps}, indent=2, sort_keys=True))
            return 0
        print(json.dumps(PROMOTION.rollback_plan(record), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Knowledge promotion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
