#!/usr/bin/env python3
"""Validate and render a client environment's non-secret target binding.

Environment files are client desired state: they may name channel/connector providers and
logical secret references, but must never contain raw credentials. This renderer fails
closed on suspicious credential fields and emits deterministic JSON for a later target
adapter to consume. It does not resolve secrets or contact providers.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import yaml

SENSITIVE_KEY = re.compile(r"(?:^|_)(?:api_?key|token|secret|password|credential|private_?key)(?:$|_)", re.I)


def _walk(value, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = (*path, key_text)
            if key_text.endswith("_ref"):
                if not isinstance(child, str) or not child.startswith("secret://") or len(child) <= len("secret://"):
                    raise ValueError(f"{'.'.join(child_path)} must be a non-empty secret:// reference")
            elif SENSITIVE_KEY.search(key_text):
                raise ValueError(f"{'.'.join(child_path)} looks like a raw credential field; use a *_ref secret:// reference")
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))


def render(path: pathlib.Path, expected_environment: str | None = None) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("environment file must contain a YAML mapping")
    environment = document.get("environment")
    if not isinstance(environment, str) or not environment:
        raise ValueError("environment must be a non-empty string")
    if expected_environment is not None and environment != expected_environment:
        raise ValueError(f"environment {environment!r} does not match expected {expected_environment!r}")
    runtime = document.get("runtime")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("profile"), str) or not runtime["profile"]:
        raise ValueError("runtime.profile must be a non-empty string")
    list(_walk(document))
    return {"apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding", "spec": document}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment_file", type=pathlib.Path)
    parser.add_argument("--expect", "--environment", dest="expect", help="expected environment name, e.g. dev or prod")
    parser.add_argument("--output", type=pathlib.Path, help="write JSON here instead of stdout")
    args = parser.parse_args()
    try:
        rendered = render(args.environment_file, args.expect)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Environment binding failed: {exc}", file=sys.stderr)
        return 1
    content = json.dumps(rendered, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
