#!/usr/bin/env python3
"""Export the governed action ledger to an append-only sink and verify the export.

  ledger-export.py export --ledger audit.jsonl --state export-state.json --sink-dir /mnt/ledger-sink
  ledger-export.py export --ledger audit.jsonl --state export-state.json --webhook https://siem.example/ingest --token-env SIEM_TOKEN
  ledger-export.py verify --ledger audit.jsonl --sink-dir /mnt/ledger-sink
  ledger-export.py verify ... --max-lag-events 100 --max-head-age-seconds 3600

`verify` exits non-zero and prints ``ALERT:`` lines for gaps in the exported sequence, a local
log that is shorter than or differs from the export, or a head manifest that does not match.
With ``--max-lag-events`` / ``--max-head-age-seconds`` it also alerts when the exporter has stalled:
the local ledger is too far ahead of the export, or events are waiting and the newest head
manifest is older than the limit. An idle, fully exported ledger never trips either.
Run `export` on a schedule under an identity that is separate from the runtime and deployment
identities; wiring the non-zero exit into monitoring is deployment-specific. A webhook sink is
write-only, so `verify` needs a readable directory sink.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_export():
    path = ROOT / "platform" / "core" / "ledger_export.py"
    spec = importlib.util.spec_from_file_location("model_garden_ledger_export", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXPORT = _load_export()


def build_sink(args: argparse.Namespace):
    if args.sink_dir:
        return EXPORT.DirectorySink(pathlib.Path(args.sink_dir))
    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not token:
        raise ValueError(f"environment variable {args.token_env} is not set")
    return EXPORT.WebhookSink(args.webhook, token=token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="export new ledger records and a head manifest")
    export.add_argument("--ledger", type=pathlib.Path, required=True)
    export.add_argument("--state", type=pathlib.Path, required=True, help="cursor file kept by the exporter")
    target = export.add_mutually_exclusive_group(required=True)
    target.add_argument("--sink-dir", help="append-only directory sink")
    target.add_argument("--webhook", help="https collector URL")
    export.add_argument("--token-env", help="environment variable holding a bearer token for the webhook")
    export.add_argument("--batch-size", type=int, default=EXPORT.DEFAULT_BATCH)
    verify = sub.add_parser("verify", help="compare a directory sink with the local ledger")
    verify.add_argument("--ledger", type=pathlib.Path, required=True)
    verify.add_argument("--sink-dir", required=True)
    verify.add_argument("--max-lag-events", type=int, help="alert when the local ledger is more than this many events ahead of the export")
    verify.add_argument("--max-head-age-seconds", type=int, help="alert when events are waiting and the newest head manifest is older than this")
    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            summary = EXPORT.export_ledger(args.ledger, build_sink(args), args.state, batch_size=args.batch_size)
            print(json.dumps(summary, sort_keys=True))
            return 0
        report = EXPORT.verify_export(
            args.ledger, EXPORT.DirectorySink(pathlib.Path(args.sink_dir)),
            max_lag_events=args.max_lag_events, max_head_age_seconds=args.max_head_age_seconds,
        )
    except EXPORT.LedgerTruncated as exc:
        print(f"ALERT: {exc}", file=sys.stderr)
        return 2
    except (EXPORT.LedgerExportError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: report[key] for key in ("ok", "localChainedEvents", "exportedRecords", "exportedHeadSequence", "lagEvents", "headAgeSeconds")}, sort_keys=True))
    for problem in report["problems"]:
        print(f"ALERT: {problem}", file=sys.stderr)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
