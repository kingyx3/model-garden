#!/usr/bin/env python3
"""Show or set live connection health, and offboard a client environment (run inside governed-tools).

  runtime-connection.py /opt/data/.modelgarden list
  runtime-connection.py /opt/data/.modelgarden set calendar revoked --actor jordan@client.example --reason "token rotated"
  runtime-connection.py /opt/data/.modelgarden offboard --actor jordan@client.example --reason "contract ended"
  runtime-connection.py /opt/data/.modelgarden offboard --agent receptionist --actor jordan@client.example
  runtime-connection.py /opt/data/.modelgarden grants list [--agent receptionist]
  runtime-connection.py /opt/data/.modelgarden grants revoke g-0123456789ab --actor jordan@client.example

Marking a connection ``revoked`` or ``failed`` takes effect on the coworker's next
``platform.capabilities`` read and is enforced at the governed boundary, without redeploying Agents.
Every change writes a chained ``operator-action`` ledger event naming the acting principal, the
capability family and the new health (never a credential, scope or account).

``offboard`` revokes every bound external (tier 1 and above) connection listed in the capability manifest
and, when a per-coworker grant registry exists (``provider-grants/<client>-<environment>.grants.json``, #129),
every grant in it; each revoked grant is ledgered. With ``--agent`` it offboards one coworker only: that
coworker's grants are revoked and every other coworker keeps working (the environment-level connection health
is left alone). It prints the steps only a person can perform: the platform cannot revoke a grant at the
provider, delete secrets from the secret store (the output lists the secret *references* of the revoked grants
so each can be deleted), erase local data or tear the stack down.

``grants list`` shows the grants without any secret reference; ``grants revoke`` revokes one.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import time

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
GRANTS = _load("model_garden_connection_grants", ROOT / "platform" / "core" / "provider_grants.py")
READINESS = _load("model_garden_connection_grant_readiness", ROOT / "platform" / "core" / "grant_readiness.py")


def external_families(manifest: dict) -> list[str]:
    """Families the manifest shows bound to an external (tier 1+) provider."""
    return sorted(
        entry["family"] for entry in manifest.get("capabilities", [])
        if isinstance(entry, dict) and entry.get("status") in ("available", "unavailable", "not-granted")
        and isinstance(entry.get("tier"), int) and entry["tier"] >= 1 and isinstance(entry.get("family"), str)
    )


def _manifest(args: argparse.Namespace, *, required: bool) -> dict:
    path = args.manifest or (
        pathlib.Path(os.environ["MODEL_GARDEN_CAPABILITY_MANIFEST"]) if os.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST") else None
    )
    if path is None or not path.is_file():
        if required:
            raise ValueError("the capability manifest is needed to know which connections are bound; pass --manifest")
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _registry(args: argparse.Namespace, manifest: dict, audit_path: pathlib.Path, *, required: bool):
    """The environment's grant registry, or ``None`` when none exists (single-Agent deployments before grants).

    ``required`` is set when the caller asked about specific grants or one coworker: an unknown environment is then an
    error rather than "no grants".
    """
    tenant = args.tenant or READINESS.tenant_of(manifest)
    if tenant is None:
        if required:
            raise ValueError("cannot tell which environment's grants to use; pass --tenant <client>-<environment> or --manifest")
        return None
    root = args.store / READINESS.REGISTRY_DIR
    if not (root / f"{tenant}.grants.json").is_file():
        return None
    return GRANTS.GrantRegistry(
        root, tenant=tenant, audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details)
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
    off = sub.add_parser("offboard", help="revoke every bound external connection and grant (or one coworker's) and print the manual steps")
    off.add_argument("--actor", required=True, help="the named person offboarding")
    off.add_argument("--reason", help="short non-secret reason")
    off.add_argument("--agent", help="offboard this coworker only: revoke its grants and leave every other coworker working")
    grants = sub.add_parser("grants", help="show or revoke per-coworker provider grants")
    grant_sub = grants.add_subparsers(dest="grants_command", required=True)
    grant_list = grant_sub.add_parser("list", help="show grants (no secret references)")
    grant_list.add_argument("--agent", help="only this coworker's grants")
    grant_revoke = grant_sub.add_parser("revoke", help="revoke one grant; no other coworker is affected")
    grant_revoke.add_argument("grant_id")
    grant_revoke.add_argument("--actor", required=True)
    for each in (off, grant_list, grant_revoke):
        each.add_argument("--manifest", type=pathlib.Path, help="capability manifest (default: MODEL_GARDEN_CAPABILITY_MANIFEST)")
        each.add_argument("--tenant", help="grant registry tenant <client>-<environment> (default: from the manifest)")
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
        if args.command == "grants":
            manifest = _manifest(args, required=False)
            registry = _registry(args, manifest, audit_path, required=True)
            if registry is None:
                print(json.dumps({"grants": [], "registry": "none"}, indent=2, sort_keys=True))
                return 0
            if args.grants_command == "list":
                print(json.dumps({"grants": registry.list(agent=args.agent)}, indent=2, sort_keys=True))
                return 0
            revoked = registry.revoke(args.grant_id, by=args.actor, now=int(time.time()))
            print(json.dumps({"revoked": {key: revoked[key] for key in ("id", "agent", "family", "provider")}}, indent=2, sort_keys=True))
            return 0
        manifest = _manifest(args, required=args.agent is None)
        agent = args.agent
        families: list[str] = []
        if agent is None:
            families = external_families(manifest)
            for family in families:
                CAPABILITIES.record_connection_health(
                    args.store, family, "revoked", actor=args.actor, reason=(args.reason or "environment offboarded")
                )
        registry = _registry(args, manifest, audit_path, required=agent is not None)
        revoked_grants = registry.revoke_all(by=args.actor, now=int(time.time()), agent=agent) if registry is not None else []
        details: dict = {"families": families}
        if registry is not None:
            details["grantsRevoked"] = len(revoked_grants)
        if agent is not None:
            details["agent"] = agent
        if args.reason:
            details["reason"] = args.reason[:200]
        GOVERN.record_operator_action(audit_path, "coworker.offboard" if agent else "environment.offboard", args.actor, details)
        print(json.dumps({
            "revoked": families,
            "grantsRevoked": [{key: item[key] for key in ("id", "agent", "family", "provider")} for item in revoked_grants],
            "secretRefsToDelete": sorted({item["secretRef"] for item in revoked_grants}),
            "manualSteps": list(OFFBOARDING_CHECKLIST),
        }, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime connection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
