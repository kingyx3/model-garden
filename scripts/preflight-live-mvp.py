#!/usr/bin/env python3
"""Fail fast on missing external resources required for the live Receptionist proof.

This preflight validates bindings only. It never prints secret values and deliberately
avoids provider API calls: scripts/reference-proof.py is the evidence-producing live
acceptance path once the required accounts/resources exist.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

REQUIREMENTS = {
    "model": ("OPENAI_API_KEY",),
    "voice": ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_SIP_TRUNK_ID"),
}


def check(env: dict[str, str], *, require_docker: bool = True) -> dict:
    missing: dict[str, list[str]] = {}
    for capability, names in REQUIREMENTS.items():
        absent = [name for name in names if not env.get(name, "").strip()]
        if absent:
            missing[capability] = absent
    # Calendar client code was retired in v0.8.0. A token alone cannot prove
    # that an upstream route supports governed booking or isolated grants.
    missing["calendar"] = ["approved upstream MCP execution and tenant proof (not implemented)"]
    if require_docker and shutil.which("docker") is None:
        missing["runtime"] = ["docker"]
    return {
        "ready": not missing,
        "missing": missing,
        "requiredCapabilities": ["model", "calendar", "voice", "runtime"],
        "optionalBindings": {
            "voice": ["MODEL_GARDEN_HUMAN_TRANSFER_TARGET", "LIVEKIT_DISPATCH_RULE_ID"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable readiness without secret values")
    parser.add_argument("--no-docker", action="store_true", help="skip the local Docker executable check")
    args = parser.parse_args()
    result = check(dict(os.environ), require_docker=not args.no_docker)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ready"]:
        print("Live MVP external-resource preflight: ready")
    else:
        print("Live MVP external-resource preflight: blocked", file=sys.stderr)
        for capability, names in sorted(result["missing"].items()):
            print(f" - {capability}: missing {', '.join(names)}", file=sys.stderr)
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
