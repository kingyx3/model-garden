#!/usr/bin/env python3
"""Read a model-completed voice call locally without rewriting original evidence (#107).

Run inside the isolated governed-tools container with a validated voice snapshot
from the matching client voice worker. By default return digests and counts only.
--include-private-text prints policy-redacted speech and model summary to the
client-host operator terminal: never use it in CI, tickets or shared logs.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
CALL_HASH = re.compile(r"[0-9a-f]{64}")


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcripts", required=True, help="voice snapshot JSONL, or - for stdin")
    parser.add_argument("--call-hash", required=True, help="exact 64-character private call digest")
    parser.add_argument("--desired-state", type=pathlib.Path, default=pathlib.Path(
        "/opt/profile-seed/.modelgarden/desired-state.json"))
    parser.add_argument("--completion-path", type=pathlib.Path, default=pathlib.Path(
        "/opt/data/.modelgarden/summary-completions/call-summary-completions.jsonl"))
    parser.add_argument("--include-private-text", action="store_true",
                        help="explicitly show policy-redacted turns and model note on this client host")
    args = parser.parse_args(argv)
    try:
        if CALL_HASH.fullmatch(args.call_hash) is None:
            raise ValueError("invalid call identifier")
        compiled = json.loads(args.desired_state.read_text(encoding="utf-8"))
        tenant = _load("completed_view_tenant", ROOT / "platform/core/tenant_identity.py")
        client = tenant.tenant_from_compiled(compiled)
        agent = (compiled.get("agent") or {}).get("metadata", {}).get("name")
        if not isinstance(agent, str) or not agent:
            raise ValueError("missing compiled agent")
        filing = _load("completed_view_filing", ROOT / "platform/channels/conversation_filing.py")
        summary = _load("completed_view_summary", ROOT / "platform/channels/call_summary.py")
        if args.transcripts == "-":
            # Bound direct stdin even when invoked outside client-operator.
            data = sys.stdin.read(16_000_001)
            if len(data.encode("utf-8")) > 16_000_000:
                raise ValueError("oversize voice snapshot")
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl") as handle:
                handle.write(data)
                handle.flush()
                records = filing.call_transcripts(filing.read_voice_records(pathlib.Path(handle.name)))
        else:
            records = filing.call_transcripts(filing.read_voice_records(pathlib.Path(args.transcripts)))
        selected = [row for row in records if row["callHash"] == args.call_hash]
        if len(selected) != 1 or selected[0].get("agent") != agent:
            raise ValueError("missing or mismatched call")
        original = selected[0]
        # Original voice records do not currently attest tenant identity.
        # Enforce any explicit tenant value and rely on the isolated deployment
        # volume for older records; do not claim independent provider identity.
        if original.get("tenant") not in (None, client):
            raise ValueError("cross-client call")
        journal = summary.SummaryCompletionJournal(args.completion_path, clock=time.time)
        view = summary.completed_call_view(original, journal, include_private_text=args.include_private_text)
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError,
            RuntimeError) as exc:
        # No private exception, transcript text, paths or identifiers in diagnostics.
        print("Completed-call view unavailable; reconcile private voice, completion and compiled identity", file=sys.stderr)
        return 1
    print(json.dumps(view, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
