#!/usr/bin/env python3
"""Render the canonical provider registry for Confluence or print JSON options."""
from __future__ import annotations
import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
path = ROOT / "platform/core/integrations.py"
spec = importlib.util.spec_from_file_location("modelgarden_integrations", path)
integrations = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(integrations)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--family", help="optional capability family filter")
    args = parser.parse_args()
    try:
        registry = integrations.load_registry()
        if args.family:
            entries = integrations.options(args.family, registry)
            registry = {**registry, "providers": entries}
        if args.format == "markdown":
            sys.stdout.write(integrations.render_directory(registry))
        else:
            print(json.dumps(registry, indent=2, sort_keys=True))
    except (OSError, ValueError) as exc:
        print(f"Integration directory failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
