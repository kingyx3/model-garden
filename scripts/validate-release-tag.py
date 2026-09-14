#!/usr/bin/env python3
"""Validate that a release tag exactly matches the repository VERSION."""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def validate(tag: str, version_file: pathlib.Path = ROOT / "VERSION") -> str:
    version = version_file.read_text(encoding="utf-8").strip()
    if not SEMVER.fullmatch(version):
        raise ValueError(f"VERSION must be plain semantic version MAJOR.MINOR.PATCH; got {version!r}")
    expected = f"v{version}"
    if tag != expected:
        raise ValueError(f"release tag {tag!r} does not match VERSION; expected {expected!r}")
    return version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="Git tag, for example v0.2.0")
    args = parser.parse_args()
    try:
        version = validate(args.tag)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Release tag {args.tag} matches VERSION {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
