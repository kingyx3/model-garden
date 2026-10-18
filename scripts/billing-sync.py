#!/usr/bin/env python3
"""Bill client usage through self-hosted Lago from the exported Action Ledgers (``platform/metering/billing.py``).

  billing-sync.py sync    --config billing.yaml [--dry-run]   send new usage to Lago (run on a timer on the billing host)
  billing-sync.py metrics --config billing.yaml               create or update the Lago billable metrics, with an agent filter
  billing-sync.py report  --config billing.yaml --since 2026-10 [--until 2026-12] [--by channel] [--format csv]

Runs as the billing host's own identity: it reads each client's export bucket as a verifier (GCE metadata token) and the Lago
API key from ``LAGO_API_KEY``, which the host loads from Secret Manager. Nothing here holds a client runtime credential.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("model_garden_billing", ROOT / "platform/metering/billing.py")
billing = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = billing
spec.loader.exec_module(billing)


def _client(config):
    key = os.environ.get("LAGO_API_KEY", "")
    if not key.strip():
        raise billing.BillingError("LAGO_API_KEY is not set")
    return billing.lago().LagoClient(key, base_url=config["lagoUrl"])


def _sinks():
    export = billing.ledger()
    token = export.metadata_token()
    return lambda source: export.GcsSink(source["bucket"], token, prefix=source["prefix"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "metrics", "report"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=pathlib.Path, required=True)
        if name == "sync":
            command.add_argument("--dry-run", action="store_true", help="read and check the exports, send and record nothing")
        if name == "report":
            command.add_argument("--since", required=True, help="first month, YYYY-MM")
            command.add_argument("--until", help="last month, YYYY-MM (default: --since)")
            command.add_argument("--by", action="append", default=[], choices=("channel", "model", "provider", "tool"))
            command.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args(argv)
    try:
        config = billing.load_config(yaml.safe_load(args.config.read_text(encoding="utf-8")))
        if args.command == "sync":
            result = billing.sync(config, sinks=_sinks(), client=None if args.dry_run else _client(config), dry_run=args.dry_run)
        elif args.command == "metrics":
            client = _client(config)
            agents = [agent for source in config["sources"] for agent in source["agents"]]
            result = {metric["code"]: client.ensure_metric(metric) for metric in billing.metric_definitions(agents)}
        else:
            rows = billing.usage_report(config["stateDir"], since=args.since, until=args.until or args.since, by=tuple(args.by))
            if args.format == "csv":
                writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else ["month"])
                writer.writeheader()
                writer.writerows(rows)
                return 0
            result = rows
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:  # BillingError and Lago/GCS errors are RuntimeErrors
        print(f"billing-sync {args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
