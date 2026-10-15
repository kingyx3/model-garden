#!/usr/bin/env python3
"""Fail when technical docs contradict the integration registry's implementation status."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    spec = importlib.util.spec_from_file_location("model_garden_status_consistency", ROOT / "platform/core/status_consistency.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        errors = module.check(ROOT)
    except (OSError, ValueError) as exc:
        print(f"Status consistency check failed: {exc}", file=sys.stderr)
        return 1
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    if errors:
        print(f"Status consistency check failed: {len(errors)} inconsistent claim(s)", file=sys.stderr)
        return 1
    print("PASS: docs agree with the integration registry's implementation status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
