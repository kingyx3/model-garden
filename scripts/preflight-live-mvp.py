#!/usr/bin/env python3
"""Fail fast on missing external resources required for the live Receptionist MVP proof.

This preflight validates presence only. It never prints secret values and deliberately
avoids provider API calls: the live proof itself remains the authority for whether a
credential/resource actually works.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

REQUIREMENTS = {
    "model": ("OPENAI_API_KEY",),
    "calendar": ("GOOGLE_CALENDAR_ID", "GOOGLE_APPLICATION_CREDENTIALS"),
    "voice": ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_SIP_TRUNK_ID"),
}


def check(env: dict[str, str], *, require_docker: bool = True) -> dict:
    missing: dict[str, list[str]] = {}
    for capability, names in REQUIREMENTS.items():
        absent = [name for name in names if not env.get(name, "").strip()]
        if absent:
            missing[capability] = absent
    if require_docker and shutil.which("docker") is None:
        missing["runtime"] = ["docker"]
    return {
        "ready": not missing,
        "missing": missing,
        "requiredCapabilities": ["model", "calendar", "voice", "runtime"],
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
