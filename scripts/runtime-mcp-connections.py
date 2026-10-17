#!/usr/bin/env python3
"""Stage, inspect or immediately disable a tenant-private MCP connection.

Runs only in the governed operator environment with access to its private state.
No credentials are supplied through this CLI and no network calls or Tool activation
occur. Activation separately requires verified owner consent, a reviewed server schema,
an independently scoped coworker grant and a vetted transport (#87, #159).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAX_MANIFEST_BYTES = 65536


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONNECTIONS = _load("modelgarden_mcp_operator_connections", ROOT / "platform/core/mcp_connections.py")
GOVERN = _load("modelgarden_mcp_operator_governance", ROOT / "scripts/govern-action.py")


def _declaration(path: pathlib.Path) -> dict:
    """Read an exact non-secret declaration; refuse special files and symlinks."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("declaration must be a regular JSON file of at most 64 KiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("declaration must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=pathlib.Path, help="private governance root, e.g. /opt/data/.modelgarden")
    parser.add_argument("--tenant", required=True, help="exact client-environment slug, e.g. acme-prod")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="list non-secret connection status for this coworker")
    status.add_argument("--agent", required=True)
    stage = sub.add_parser("stage", help="record reviewed manifest as inactive; never makes a network connection")
    stage.add_argument("--agent", required=True)
    stage.add_argument("--id", required=True, dest="connection_id")
    stage.add_argument("--file", type=pathlib.Path, required=True, dest="declaration_file")
    stage.add_argument("--actor", required=True)
    stage.add_argument("--replace", action="store_true", help="replace a staged or active declaration and deactivate it")
    disable = sub.add_parser("disable", help="disable one coworker's connection immediately")
    disable.add_argument("--agent", required=True)
    disable.add_argument("--id", required=True, dest="connection_id")
    disable.add_argument("--actor", required=True)
    args = parser.parse_args(argv)

    try:
        store = CONNECTIONS.McpConnectionStore(
            args.store / "mcp-connections", tenant=args.tenant,
            audit=lambda action, by, details: GOVERN.record_operator_action(
                args.store / "audit.jsonl", action, by, details
            ),
        )
        if args.command == "status":
            outcome = {"connections": store.list_public(agent=args.agent)}
        elif args.command == "stage":
            declaration = _declaration(args.declaration_file)
            outcome = store.stage(args.connection_id, agent=args.agent, declaration=declaration,
                                  by=args.actor, now=int(time.time()), replace=args.replace)
        else:
            outcome = store.disable(args.connection_id, agent=args.agent, by=args.actor)
        print(json.dumps(outcome, indent=2, sort_keys=True))
        return 0
    except (CONNECTIONS.ConnectionRefused, ValueError, OSError, json.JSONDecodeError) as exc:
        # Never print the manifest or raw credential; only controlled error descriptions.
        print(f"MCP operator command failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
