#!/usr/bin/env python3
"""Inspect and review *untrusted* connector candidates (never install/grant them).

Examples:
  python3 scripts/connector-candidates.py --root .modelgarden/candidates refresh --search hubspot
  python3 scripts/connector-candidates.py --root .modelgarden/candidates list
  python3 scripts/connector-candidates.py --root .modelgarden/candidates review <candidate-id> --by reviewer@client.example
  python3 scripts/connector-candidates.py --root .modelgarden/candidates deny <upstream-name> --reason malicious --by reviewer@client.example

Public data remains distinct from contracts/v1/integrations.json (sole go-live authority).
Vendor-maintained catalog exports may be inspected with `import-json` but are
treated identically as untrusted discovery, not as authorization.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CANDIDATES = _load("model_garden_candidate_cli", ROOT / "platform/core/connector_candidates.py")
LOOKUP = _load("model_garden_registry_lookup_cli", ROOT / "platform/core/registry_lookup.py")
INTEGRATIONS = _load("model_garden_integrations_candidate_cli", ROOT / "platform/core/integrations.py")


def refresh(book, query: str, *, discover=LOOKUP.discover, now: int | None = None) -> dict:
    """A failed registry request is recorded but never changes last good candidates."""
    current = int(time.time()) if now is None else now
    try:
        found = discover(query, now=current)
        candidates, skipped = [], found["skipped"]
        for raw in found["entries"]:
            try:
                candidates.append(CANDIDATES.normalise(raw, source="mcp-registry", fetched_at=current))
            except (ValueError, TypeError):
                skipped += 1
        changes = book.ingest(candidates, source="mcp-registry", now=current)
        return {"status": "ok", "counts": {key: len(value) for key, value in changes.items()},
                "skipped": skipped, "pages": found["pages"], "truncated": found["truncated"],
                "feed": book.feed_state("mcp-registry", now=current)}
    except (LOOKUP.RegistryLookupError, OSError, ValueError) as exc:
        kind = str(exc) if isinstance(exc, LOOKUP.RegistryLookupError) and str(exc) in (
            "rate-limited", "network-error", "invalid-feed", "invalid-pagination", "invalid-json",
            "response-too-large", "oversized-page", "http-error"
        ) else "lookup-error"
        book.feed_failed("mcp-registry", kind, now=current)
        return {"status": "unavailable", "reason": kind, "feed": book.feed_state("mcp-registry", now=current)}


def import_snapshot(book, path: pathlib.Path, source: str, *, now: int) -> dict:
    """Import a provider-maintained JSON export supplied by the operator; trust level stays discovered."""
    if source == "mcp-registry":
        raise ValueError("use refresh for the official registry; vendor source must name its publisher")
    feed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(feed, dict) or not isinstance(feed.get("servers"), list):
        raise ValueError("vendor snapshot must contain a servers array")
    if len(feed["servers"]) > CANDIDATES.MAX_CANDIDATES:
        raise ValueError("vendor snapshot is too large")
    candidates = [CANDIDATES.normalise(item, source=source, fetched_at=now) for item in feed["servers"]]
    changes = book.ingest(candidates, source=source, now=now)
    return {"status": "ok", "counts": {key: len(value) for key, value in changes.items()}, "source": source}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=pathlib.Path, required=True, help="private candidate book directory; not an Agent workspace")
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("refresh", help="look up official MCP registry (read-only, bounded, no sync job)")
    discover.add_argument("--search", required=True, help="upstream server name fragment")
    vendor = sub.add_parser("import-json", help="import a vendor-maintained server catalog export, untrusted")
    vendor.add_argument("file", type=pathlib.Path)
    vendor.add_argument("--source", required=True, help="vendor/publisher source identifier")
    sub.add_parser("list", help="show candidates alongside the canonical implementation statuses")
    for name, gate in (("review", "reviewed"), ("contract-tested", "contract-tested")):
        cmd = sub.add_parser(name, help=f"advance a vetted candidate to {gate}")
        cmd.add_argument("id")
        cmd.add_argument("--by", required=True)
        if name == "contract-tested":
            cmd.add_argument("--digest", required=True, help="SHA-256 of independent passing contract-test evidence")
    deny = sub.add_parser("deny", help="deny-list an upstream name across future refreshes")
    deny.add_argument("name")
    deny.add_argument("--reason", required=True)
    deny.add_argument("--by", required=True)
    link = sub.add_parser("link", help="reference an existing canonical integration ID (does not grant/approve)")
    link.add_argument("id")
    link.add_argument("provider")
    link.add_argument("--by", required=True)
    args = parser.parse_args(argv)
    try:
        book = CANDIDATES.CandidateBook(args.root)
        now = int(time.time())
        if args.command == "refresh":
            result = refresh(book, args.search, now=now)
        elif args.command == "import-json":
            result = import_snapshot(book, args.file, args.source, now=now)
        elif args.command == "list":
            result = {"candidates": book.directory(INTEGRATIONS.load_registry()),
                      "feed": book.feed_state("mcp-registry", now=now),
                      "authority": "contracts/v1/integrations.json"}
        elif args.command in ("review", "contract-tested"):
            result = book.advance(args.id, "reviewed" if args.command == "review" else "contract-tested",
                                  by=args.by, now=now, contract_test_digest=getattr(args, "digest", None))
        elif args.command == "deny":
            result = {"denied": book.deny(args.name, args.reason, by=args.by, now=now)}
        else:
            book.link(args.id, args.provider, by=args.by)
            result = {"linked": args.id, "provider": args.provider, "approved": False}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") != "unavailable" else 1
    except (OSError, ValueError) as exc:
        print("Connector discovery failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
