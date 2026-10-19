#!/usr/bin/env python3
"""Review and approve the exact operations of a client's own REST API (#159, #89).

Runs only in the governed operator environment with access to its private state, for example
``docker exec governed-tools python /opt/model-garden-platform/scripts/runtime-custom-api.py /opt/data/.modelgarden --tenant acme-prod status``.
It makes no network call and takes no credential. ``status`` lists every declared operation with its method, path, risk and whether
it is approved at its current definition; ``approve`` pins one operation's current definition under the named reviewer, so a later
change to its method, path or schema hides it again until it is approved again. An operation whose own description or schema looks
unsafe cannot be approved. The model is offered an operation only when it is approved here, compiled as an ``http`` Tool of the
same risk, and the deployment installed the custom API sender.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DEFINITIONS = pathlib.Path("/opt/profile-seed/.modelgarden/custom-apis.json")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


API = _load("modelgarden_operator_custom_api", ROOT / "platform/core/custom_api.py")
PASSTHROUGH = _load("modelgarden_operator_api_passthrough", ROOT / "platform/core/mcp_passthrough.py")
GOVERN = _load("modelgarden_operator_api_governance", ROOT / "scripts/govern-action.py")


def definitions(path: pathlib.Path) -> dict[str, dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError("definitions must be a regular JSON file of at most 1 MiB")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise ValueError("definitions must be a list")
    normal = [API.validate_definition(item) for item in loaded]
    return {item["name"]: item for item in normal}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=pathlib.Path, help="private governance root, e.g. /opt/data/.modelgarden")
    parser.add_argument("--tenant", required=True, help="exact client-environment slug, e.g. acme-prod")
    parser.add_argument("--definitions", type=pathlib.Path, default=DEFAULT_DEFINITIONS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="every declared operation with its risk and approval state")
    approve = sub.add_parser("approve", help="pin one operation's current definition")
    approve.add_argument("--api", required=True)
    approve.add_argument("--operation", required=True)
    approve.add_argument("--actor", required=True, help="the named person who reviewed it")
    args = parser.parse_args(argv)
    try:
        declared = definitions(args.definitions)
        approvals = PASSTHROUGH.SchemaApprovals(
            args.store / "api-approvals", tenant=args.tenant,
            audit=lambda action, by, details: GOVERN.record_operator_action(args.store / "audit.jsonl", action, by, details),
        )
        if args.command == "status":
            rows = []
            for name, definition in sorted(declared.items()):
                review = API.review(definition)["operations"]
                for tool in API.operation_tools(definition):
                    op_id = tool["name"]
                    operation = definition["operations"][op_id]
                    rows.append({"api": name, "operation": op_id, "tool": f"api.{name}.{op_id}", "method": operation["method"], "path": operation["path"],
                                 "risk": operation["risk"], "state": approvals.status(name, tool), "unsafe": bool(review[op_id]["blocking"])})
            print(json.dumps({"operations": rows}, indent=2, sort_keys=True))
            return 0
        definition = declared.get(args.api)
        match = next((item for item in API.operation_tools(definition) if item["name"] == args.operation), None) if definition else None
        if match is None:
            print("that API operation is not declared", file=sys.stderr)
            return 1
        fingerprint = approvals.approve(args.api, match, by=args.actor, now=int(time.time()))
        print(json.dumps({"approved": f"api.{args.api}.{args.operation}", "fingerprint": fingerprint, "by": args.actor}, sort_keys=True))
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
