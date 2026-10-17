#!/usr/bin/env python3
"""Create an owner key and sign a Policy rule, so a relaxing rule is authenticated and not just named (#114).

``keygen`` prints a key pair: keep the private half where only the owner has it and put the public half in the EnvironmentBinding
under ``governance.ownerKeys`` (owner identity to key). ``sign`` prints the rule with the owner's ``signoff`` (name, content digest
and Ed25519 signature) ready to paste into the Policy file; the compiler then rejects a relaxing rule that was edited after signing
or signed by anyone else. This script makes no network calls and the private key is read from a file or the environment, never
from a command-line argument.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("model_garden_policy_for_signoff", ROOT / "platform" / "core" / "policy.py")
POLICY = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = POLICY
_spec.loader.exec_module(POLICY)  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen", help="print a new owner key pair as JSON")
    sign = commands.add_parser("sign", help="print a Policy rule with the owner's signed sign-off")
    sign.add_argument("policy", type=pathlib.Path, help="a Policy resource file")
    sign.add_argument("--rule", required=True, help="the rule id to sign")
    sign.add_argument("--owner", required=True, help="the owner identity listed in governance.ownerKeys")
    sign.add_argument("--key-file", type=pathlib.Path, help="file holding the base64 private key (or set MODEL_GARDEN_OWNER_KEY)")
    args = parser.parse_args(argv)
    if args.command == "keygen":
        print(json.dumps(POLICY.generate_owner_key(), indent=2))
        return 0
    private = args.key_file.read_text(encoding="utf-8").strip() if args.key_file else os.environ.get("MODEL_GARDEN_OWNER_KEY", "")
    document = yaml.safe_load(args.policy.read_text(encoding="utf-8"))
    rule = next((item for item in document.get("spec", {}).get("rules", []) if item.get("id") == args.rule), None)
    if rule is None:
        print(f"no rule {args.rule!r} in {args.policy}", file=sys.stderr)
        return 1
    try:
        print(yaml.safe_dump(POLICY.sign_rule(rule, args.owner, private), sort_keys=False))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
