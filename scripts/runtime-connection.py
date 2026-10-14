#!/usr/bin/env python3
"""Show or set live connection health for a running client environment.

Marking a connection ``revoked`` or ``failed`` takes effect on the coworker's next
``platform.capabilities`` read, without redeploying Agents.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_capabilities():
    path = ROOT / "platform" / "core" / "capabilities.py"
    module_spec = importlib.util.spec_from_file_location("model_garden_connection_capabilities", path)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


CAPABILITIES = _load_capabilities()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=pathlib.Path, help="governance data root, e.g. /opt/data/.modelgarden")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show recorded connection health")
    mark = sub.add_parser("set", help="record one connection's health")
    mark.add_argument("family", help="capability family, e.g. calendar")
    mark.add_argument("health", choices=CAPABILITIES.CONNECTION_HEALTH)
    mark.add_argument("--actor", required=True, help="operator identity recording the change")
    mark.add_argument("--reason", help="short non-secret reason")
    args = parser.parse_args()
    try:
        if args.command == "list":
            print(json.dumps(CAPABILITIES.read_connection_health(args.store), indent=2, sort_keys=True))
            return 0
        record = CAPABILITIES.record_connection_health(
            args.store, args.family, args.health, actor=args.actor, reason=args.reason
        )
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Runtime connection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
