#!/usr/bin/env python3
"""Credential-free preflight for a reference receptionist's *configured* live prerequisites.

This reads approved provider evidence and a tenant-scoped, per-coworker grant; it
does not call a provider, inspect secret values, attest the running process, or
establish that any tenant transaction occurred. Only independently retained
provider receipts and the reference proof collector can establish live acceptance.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import pathlib
import shutil
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CALENDAR_TOOLS = ("calendar.availability", "calendar.book")
UPSTREAM_METHODS = frozenset(("governed-mcp", "managed", "self-hosted", "openapi", "governed-http"))
REQUIREMENTS = {
    "model": ("OPENAI_API_KEY",),
    "voice": ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_SIP_TRUNK_ID"),
}


def _module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _calendar_blockers(
    provider: str | None, *, evidence_root: pathlib.Path,
    grant_root: pathlib.Path | None, tenant: str | None,
    agent: str | None, connection_id: str | None, now: dt.datetime,
) -> list[str]:
    """Return non-secret blockers. This is a local configuration check, not provider attestation."""
    if not provider:
        return ["select an approved upstream calendar provider with --calendar-provider; no legacy token unlocks booking"]
    if not (grant_root and tenant and agent and connection_id):
        return ["specify --grant-root, --tenant, --agent and --calendar-connection-id for the coworker's isolated grant"]

    try:
        evidence = _module("preflight_provider_evidence", "platform/core/provider_evidence.py")
        root = pathlib.Path(evidence_root)
        manifest = evidence.manifest(root, now=now)
        entries = [entry for entry in manifest["providers"] if entry["id"] == provider]
        if len(entries) != 1:
            return ["selected calendar provider is not in the reviewed integration registry"]
        entry = entries[0]
        if entry["family"] != "calendar" or entry["tier"] < 1 or entry["method"] not in UPSTREAM_METHODS:
            return ["selected provider is not an approved external upstream calendar route"]
        if not set(CALENDAR_TOOLS).issubset(entry["tools"]):
            return ["selected provider does not declare both calendar availability and booking"]
        registry = json.loads((root / "contracts/v1/integrations.json").read_text(encoding="utf-8"))
        registered = next(item for item in registry["providers"] if item["id"] == provider)
        scoped = registered.get("scopeByTool")
        if not isinstance(scoped, dict) or any(
            not isinstance(scoped.get(tool), list) or not scoped[tool]
            or not all(isinstance(scope, str) and scope.strip() for scope in scoped[tool])
            for tool in CALENDAR_TOOLS
        ):
            return ["selected calendar route has no reviewed scopes for each required operation"]
        if entry["status"] != "live" or entry["liveProof"]["state"] != "valid" or not entry["goLive"]["allowed"] or not entry["adapter"]["sha256"]:
            return ["calendar provider is not live-qualified with current adapter, tests, scopes and retained tenant proof"]

        grants = _module("preflight_provider_grants", "platform/core/provider_grants.py")
        grant_root = pathlib.Path(grant_root)
        if not grant_root.is_dir() or not (grant_root / f"{tenant}.grants.json").is_file():
            return ["the selected tenant has no initialized private provider grant registry"]
        registry = grants.GrantRegistry(grant_root, tenant=tenant)
        accesses = [
            registry.resolve(
                attested_agent=agent, family="calendar", tool=tool,
                originator_permissions=CALENDAR_TOOLS, enabled_tools=CALENDAR_TOOLS,
                connection_id=connection_id, now=int(now.timestamp()),
            )
            for tool in CALENDAR_TOOLS
        ]
        if any(
            access["provider"] != provider or access["connectionId"] != connection_id
            or access["isolation"] not in grants.SUPPORTED_ISOLATIONS or access["degraded"]
            for access in accesses
        ) or len({access["grantId"] for access in accesses}) != 1:
            return ["calendar operations lack one healthy, matching, independently isolated coworker grant"]
    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError):
        # Provider/grant records and their paths are not a safe error-reporting surface.
        return ["calendar provider evidence or coworker grant is missing, invalid, expired or unreadable"]
    return []


def check(
    env: dict[str, str], *, require_docker: bool = True,
    calendar_provider: str | None = None, evidence_root: pathlib.Path = ROOT,
    grant_root: pathlib.Path | None = None, tenant: str | None = None,
    agent: str | None = None, connection_id: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    missing: dict[str, list[str]] = {}
    for capability, names in REQUIREMENTS.items():
        absent = [name for name in names if not env.get(name, "").strip()]
        if absent:
            missing[capability] = absent
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("preflight time must be timezone-aware")
    calendar = _calendar_blockers(
        calendar_provider, evidence_root=evidence_root, grant_root=grant_root,
        tenant=tenant, agent=agent, connection_id=connection_id, now=moment,
    )
    if calendar:
        missing["calendar"] = calendar
    if require_docker and shutil.which("docker") is None:
        missing["runtime"] = ["docker"]
    return {
        "ready": not missing,
        "missing": missing,
        "requiredCapabilities": ["model", "calendar", "voice", "runtime"],
        "optionalBindings": {
            "voice": ["MODEL_GARDEN_HUMAN_TRANSFER_TARGET", "LIVEKIT_DISPATCH_RULE_ID"],
        },
        "proofBoundary": "configuration preflight only; no provider call, running-grant attestation or live tenant proof was executed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable readiness without secret values")
    parser.add_argument("--no-docker", action="store_true", help="skip the local Docker executable check")
    parser.add_argument("--calendar-provider", help="exact live-qualified upstream provider ID from the integration registry")
    parser.add_argument("--grant-root", type=pathlib.Path, help="private client-scoped provider grant registry directory")
    parser.add_argument("--tenant", help="client-environment tenant slug for the grant registry")
    parser.add_argument("--agent", help="coworker Agent ID; must match the deployed runtime's attested identity")
    parser.add_argument("--calendar-connection-id", help="exact coworker connection ID to verify")
    args = parser.parse_args(argv)
    result = check(
        dict(os.environ), require_docker=not args.no_docker,
        calendar_provider=args.calendar_provider, grant_root=args.grant_root,
        tenant=args.tenant, agent=args.agent, connection_id=args.calendar_connection_id,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ready"]:
        print("Live MVP configuration preflight: eligible; live provider/tenant acceptance still required")
    else:
        print("Live MVP external-resource preflight: blocked", file=sys.stderr)
        for capability, names in sorted(result["missing"].items()):
            print(f" - {capability}: missing {', '.join(names)}", file=sys.stderr)
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
