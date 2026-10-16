#!/usr/bin/env python3
"""Emit the per-provider evidence manifest and check or decide go-live claims (reads files only; no provider is called).

  provider-evidence.py                       # JSON manifest on standard output
  provider-evidence.py --format markdown     # table for review or the integration directory
  provider-evidence.py --output evidence.json
  provider-evidence.py --check               # fail if the registry's claims are untrue now (used by `make contracts`)
  provider-evidence.py --go-live google-calendar   # exit 1 with the blockers when a go-live claim is not allowed

Live tenant proof is a retained, redacted file at assurance/provider-proof/<provider id>.json bound to the adapter it proved
and to an expiry; see platform/core/provider_evidence.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("model_garden_provider_evidence", ROOT / "platform" / "core" / "provider_evidence.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=pathlib.Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=pathlib.Path, help="write the manifest here instead of standard output")
    parser.add_argument("--check", action="store_true", help="fail when a live claim has no valid proof file or a proof is malformed")
    parser.add_argument("--go-live", metavar="PROVIDER", help="exit 1 with the blockers unless a go-live claim is allowed")
    parser.add_argument("--now", help="ISO 8601 time to evaluate proof expiry at (default: now; for tests and reviews)")
    args = parser.parse_args(argv)
    try:
        module = _module()
        now = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
        if now is not None and now.tzinfo is None:
            raise ValueError("--now needs a time zone, for example 2026-10-16T00:00:00Z")
        if args.check:
            errors = module.check(args.root, now=now)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            if errors:
                print(f"Provider evidence check failed: {len(errors)} problem(s)", file=sys.stderr)
                return 1
            print("PASS: every live provider claim has valid retained proof; no proof file is malformed")
            return 0
        if args.go_live:
            verdict = module.go_live(args.go_live, args.root, now=now)
            print(json.dumps(verdict, indent=2, sort_keys=True))
            return 0 if verdict["allowed"] else 1
        data = module.manifest(args.root, now=now)
        text = module.render_markdown(data) if args.format == "markdown" else json.dumps(data, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Provider evidence failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
