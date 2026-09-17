#!/usr/bin/env python3
"""Validate the production operating record required before a client is marked ready."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import yaml

REQUIRED_PATHS = (
    "ownership.runtime_operator",
    "ownership.cloud_owner",
    "ownership.model_provider_owner",
    "ownership.voice_provider_owner",
    "ownership.business_system_owner",
    "access.break_glass_owner",
    "access.recovery_contact",
    "support.primary_channel",
    "support.escalation_owner",
    "support.review_cadence",
    "data.retention_policy",
    "data.recording_transcription_policy",
    "data.vendor_handling_review",
    "continuity.operator_backup",
    "continuity.access_recovery_path",
    "continuity.rollback_test_record",
    "continuity.backup_restore_requirement",
)


def _get(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def validate(value: dict[str, Any]) -> dict[str, Any]:
    missing = []
    for path in REQUIRED_PATHS:
        item = _get(value, path)
        if item is None or (isinstance(item, str) and not item.strip()) or item == "REQUIRED":
            missing.append(path)
    return {"ready": not missing, "missing": missing, "required": list(REQUIRED_PATHS)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=pathlib.Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        value = yaml.safe_load(args.record.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("operations record must be a YAML mapping")
        result = validate(value)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Operations readiness failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ready"]:
        print("Production operating record: ready")
    else:
        print("Production operating record: incomplete", file=sys.stderr)
        for path in result["missing"]:
            print(f" - {path}", file=sys.stderr)
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
