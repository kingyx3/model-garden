#!/usr/bin/env python3
"""Show everything waiting for a person on this runtime (run inside governed-tools).

  runtime-inbox.py            # text
  runtime-inbox.py --json

Lists pending approvals, open hand-offs (overdue first) and counts of contact records to review. It names
Tools, assignees, deadlines and IDs but never action arguments, summaries or personal data; the `next`
command on each line shows the exact content to the person allowed to act.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--data-root", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_GOVERNANCE_ROOT", "/opt/data/.modelgarden")))
    args = parser.parse_args(argv)
    try:
        spec = importlib.util.spec_from_file_location("runtime_inbox_core", ROOT / "platform" / "core" / "inbox.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules["runtime_inbox_core"] = module
        spec.loader.exec_module(module)
        inbox = module.collect(
            approvals=args.data_root / "approvals", handoffs=args.data_root / "handoffs", contacts=args.data_root / "contacts",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime inbox failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(inbox, indent=2, sort_keys=True) + "\n" if args.json else module.format_text(inbox))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
