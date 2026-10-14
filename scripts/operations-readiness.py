#!/usr/bin/env python3
"""Validate the production operating record required before a client is marked ready."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import yaml

# Every mandatory operator, consent, monitoring, recovery and acceptance field in the
# canonical template must carry a substantive record, not an unfilled placeholder.
REQUIRED_PATHS = (
    "client",
    "ownership.runtime_operator",
    "ownership.cloud_owner",
    "ownership.model_provider_owner",
    "ownership.voice_provider_owner",
    "ownership.business_system_owner",
    "access.break_glass_owner",
    "access.recovery_contact",
    "access.credential_revocation_record",
    "support.primary_channel",
    "support.escalation_owner",
    "support.review_cadence",
    "support.contracted_coverage",
    "monitoring.alert_destination",
    "data.retention_policy",
    "data.recording_transcription_policy",
    "data.vendor_handling_review",
    "data.disclosure_requirements",
    "continuity.operator_backup",
    "continuity.access_recovery_path",
    "continuity.rollback_test_record",
    "continuity.backup_restore_requirement",
    "continuity.offboarding_owner",
    "acceptance.keyless_redeploy_evidence",
    "acceptance.real_model_turn_evidence",
    "acceptance.governed_business_action_evidence",
    "acceptance.inbound_voice_evidence",
    "acceptance.human_fallback_evidence",
    "acceptance.production_rollback_evidence",
)
PLACEHOLDERS = {"REQUIRED", "TODO", "TBD", "PENDING", "N/A", "UNKNOWN"}


def _get(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _complete(item: Any) -> bool:
    return isinstance(item, str) and bool(item.strip()) and item.strip().upper() not in PLACEHOLDERS


def _unresolved(value: Any, path: str = "") -> set[str]:
    """Reject placeholder strings anywhere, including newly introduced template keys."""
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            paths.update(_unresolved(item, child))
        return paths
    if isinstance(value, list):
        paths = set()
        for index, item in enumerate(value):
            paths.update(_unresolved(item, f"{path}[{index}]"))
        return paths
    if isinstance(value, str) and value.strip().upper() in PLACEHOLDERS:
        return {path}
    return set()


def validate(value: dict[str, Any]) -> dict[str, Any]:
    missing = {path for path in REQUIRED_PATHS if not _complete(_get(value, path))}
    missing.update(_unresolved(value))
    return {"ready": not missing, "missing": sorted(missing), "required": list(REQUIRED_PATHS)}


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
