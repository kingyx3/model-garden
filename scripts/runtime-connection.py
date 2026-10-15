#!/usr/bin/env python3
"""Show or set live connection health, and offboard a client environment (run inside governed-tools).

  runtime-connection.py /opt/data/.modelgarden list
  runtime-connection.py /opt/data/.modelgarden set calendar revoked --actor jordan@client.example --reason "token rotated"
  runtime-connection.py /opt/data/.modelgarden offboard --actor jordan@client.example --reason "contract ended"

Marking a connection ``revoked`` or ``failed`` takes effect on the coworker's next
``platform.capabilities`` read and is enforced at the governed boundary, without redeploying Agents.
Every change writes a chained ``operator-action`` ledger event naming the acting principal, the
capability family and the new health (never a credential, scope or account).

``offboard`` revokes every bound external (tier 1 and above) connection listed in the capability manifest
and prints the steps only a person can perform: the platform cannot revoke a grant at the provider, delete
secrets from the secret store, erase local data or tear the stack down.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

OFFBOARDING_CHECKLIST = (
    "Revoke each provider authorization at the provider (the Google or Microsoft admin consent, API keys, OAuth grants): "
    "revoking here only stops the coworker using them.",
    "Delete this environment's secrets from the secret store (the GitHub Environment secrets or the cloud secret manager).",
    "Export and verify the action ledger before anything is deleted: scripts/ledger-export.py export, then verify.",
    "Decide what happens to local data under the client's retention rules: the contact log (client-operator.py contacts), "
    "call transcripts and fallback messages, hand-offs and approvals; erase what must not be kept.",
    "Stop and remove the stack and its volumes (docker compose down -v) and, for a cloud project, run the Terraform destroy.",
)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CAPABILITIES = _load("model_garden_connection_capabilities", ROOT / "platform" / "core" / "capabilities.py")
GOVERN = _load("model_garden_connection_govern", ROOT / "scripts" / "govern-action.py")


def external_families(manifest: dict) -> list[str]:
    """Families the manifest shows bound to an external (tier 1+) provider."""
    return sorted(
        entry["family"] for entry in manifest.get("capabilities", [])
        if isinstance(entry, dict) and entry.get("status") in ("available", "unavailable", "not-granted")
        and isinstance(entry.get("tier"), int) and entry["tier"] >= 1 and isinstance(entry.get("family"), str)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="governance data root, e.g. /opt/data/.modelgarden")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show recorded connection health")
    mark = sub.add_parser("set", help="record one connection's health")
    mark.add_argument("family", help="capability family, e.g. calendar")
    mark.add_argument("health", choices=CAPABILITIES.CONNECTION_HEALTH)
    mark.add_argument("--actor", required=True, help="operator identity recording the change")
    mark.add_argument("--reason", help="short non-secret reason")
    off = sub.add_parser("offboard", help="revoke every bound external connection and print the manual steps")
    off.add_argument("--actor", required=True, help="the named person offboarding this environment")
    off.add_argument("--reason", help="short non-secret reason")
    off.add_argument("--manifest", type=pathlib.Path, help="capability manifest (default: MODEL_GARDEN_CAPABILITY_MANIFEST)")
    args = parser.parse_args(argv)
    audit_path = args.audit_path or args.store / "audit.jsonl"
    try:
        if args.command == "list":
            print(json.dumps(CAPABILITIES.read_connection_health(args.store), indent=2, sort_keys=True))
            return 0
        if args.command == "set":
            record = CAPABILITIES.record_connection_health(
                args.store, args.family, args.health, actor=args.actor, reason=args.reason
            )
            details = {"family": args.family, "health": args.health}
            if args.reason:
                details["reason"] = args.reason[:200]
            GOVERN.record_operator_action(audit_path, "connection.set", args.actor, details)
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0
        manifest_path = args.manifest or (
            pathlib.Path(os.environ["MODEL_GARDEN_CAPABILITY_MANIFEST"]) if os.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST") else None
        )
        if manifest_path is None or not manifest_path.is_file():
            raise ValueError("the capability manifest is needed to know which connections are bound; pass --manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        families = external_families(manifest if isinstance(manifest, dict) else {})
        for family in families:
            CAPABILITIES.record_connection_health(
                args.store, family, "revoked", actor=args.actor, reason=(args.reason or "environment offboarded")
            )
        details = {"families": families}
        if args.reason:
            details["reason"] = args.reason[:200]
        GOVERN.record_operator_action(audit_path, "environment.offboard", args.actor, details)
        print(json.dumps({"revoked": families, "manualSteps": list(OFFBOARDING_CHECKLIST)}, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime connection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
