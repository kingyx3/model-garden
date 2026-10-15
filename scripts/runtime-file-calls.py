#!/usr/bin/env python3
"""File stored call transcripts into the bound CRM through the governed boundary (run inside governed-tools).

  runtime-file-calls.py --transcripts /path/transcripts.jsonl
  cat transcripts.jsonl | runtime-file-calls.py --transcripts -

Each call becomes one `crm.log-interaction` against the `unmatched` queue (a caller's number is not a
verified identity), referenced by `transcript:<callHash>`, with the outcome and size of the call and the
model summary when the record has one, never the transcript text. Every filing is ledgered under the
identity `runtime:conversation-filing`. Re-running is safe: a reference already filed anywhere is skipped.
A person then assigns unmatched records with `client-operator.py contacts review|assign`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILING_IDENTITY = "runtime:conversation-filing"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transcripts", required=True, help="transcripts JSONL path, or - for standard input")
    parser.add_argument("--desired-state", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--audit-path", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")))
    parser.add_argument("--approval-root", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_APPROVAL_ROOT", "/opt/data/.modelgarden/approvals")))
    args = parser.parse_args(argv)
    try:
        runtime_module = _load("runtime_file_calls_governed_mcp", ROOT / "platform" / "runtime" / "governed_mcp.py")
        filing = _load("runtime_file_calls_filing", ROOT / "platform" / "channels" / "conversation_filing.py")
        runtime = runtime_module.GovernedToolRuntime(
            args.desired_state, args.audit_path, args.approval_root,
            environ={**os.environ, "MODEL_GARDEN_INITIATING_IDENTITY": FILING_IDENTITY},
        )
        store = runtime.crm_store()
        if args.transcripts == "-":
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl") as handle:
                handle.write(sys.stdin.read())
                handle.flush()
                records = filing.read_call_transcripts(pathlib.Path(handle.name))
        else:
            records = filing.read_call_transcripts(pathlib.Path(args.transcripts))
        failures: list[str] = []
        totals = filing.file_call_transcripts(runtime, store, records, log=failures.append)
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        print(f"Runtime file-calls failed: {exc}", file=sys.stderr)
        return 1
    for failure in failures:
        print(f"failed to file {failure}", file=sys.stderr)
    print(json.dumps({"records": len(records), **totals}, sort_keys=True))
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
