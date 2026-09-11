#!/usr/bin/env python3
"""Validate Model Garden YAML resources against versioned JSON Schemas."""

from __future__ import annotations

import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts" / "v1"
SCHEMAS = {
    "Agent": "agent.schema.json",
    "Skill": "skill.schema.json",
    "ModelProfile": "model-profile.schema.json",
    "Tool": "tool.schema.json",
    "KnowledgeSource": "knowledge-source.schema.json",
    "Policy": "policy.schema.json",
}


def load_validator(kind: str) -> Draft202012Validator:
    path = SCHEMA_DIR / SCHEMAS[kind]
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def main() -> int:
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "examples/workspace")
    validators = {kind: load_validator(kind) for kind in SCHEMAS}
    errors: list[str] = []
    validated = 0

    for path in sorted(target.rglob("*.yaml")):
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict) or "kind" not in document:
            continue
        kind = document.get("kind")
        if kind not in validators:
            errors.append(f"{path}: unsupported kind {kind!r}")
            continue
        for error in sorted(validators[kind].iter_errors(document), key=lambda e: list(e.path)):
            location = ".".join(str(part) for part in error.path) or "<root>"
            errors.append(f"{path}:{location}: {error.message}")
        validated += 1

    if validated == 0:
        errors.append(f"No Model Garden resources found under {target}")

    if errors:
        print("Workspace validation failed:", file=sys.stderr)
        for error in errors:
            print(f" - {error}", file=sys.stderr)
        return 1

    print(f"Validated {validated} Model Garden resource(s) under {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
