#!/usr/bin/env python3
"""File stored call transcripts into the bound CRM through the governed boundary (run inside governed-tools).

  runtime-file-calls.py --transcripts /path/transcripts.jsonl
  cat transcripts.jsonl | runtime-file-calls.py --transcripts -

Each call becomes one `crm.log-interaction` against the `unmatched` queue (a caller's number is not a
verified identity), referenced by `transcript:<callHash>`, with the outcome and size of the call and the
model summary when the record has one, never the transcript text. Every filing is ledgered under the
identity `runtime:conversation-filing`. Re-running is safe: a reference already filed anywhere is skipped.
A person then assigns unmatched records with `client-operator.py contacts review|assign`.

Each call is also put on the cross-channel conversation timeline (`platform/core/conversations.py`) by reference only, so a later
email from the same verified sender can join the same customer's history; see `client-operator.py conversations`. That is
best-effort and never changes how a call was filed.

Before filing, each call not yet filed gets a post-call summary (`platform/channels/call_summary.py`): written by the coworker through
the Hermes API when `MODEL_GARDEN_COWORKER_API_KEY` and a coworker URL (`MODEL_GARDEN_COWORKER_API_URL`, else `coworker.url` in the worker
config) are present, otherwise a deterministic extractive fallback. The summary is what `crm.log-interaction` carries, so an approval shows
it exactly, and the timeline records its digest and source. `--no-summary` files the plain outcome-and-size line instead.

The same input may also hold the worker's lifecycle events: each `TranscriptWriteFailed` (a call whose transcript
could not be stored) is ledgered once as `transcript.write-failed` with the error type and time only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
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


class _CountingRecorder:
    """The conversation recorder, counting what it put on the timeline so the command can report it (never the content).

    With no recorder (no compiled client to scope the timeline to) every call is counted as skipped and filing is unaffected.
    """

    def __init__(self, recorder) -> None:
        self._recorder = recorder
        self.counts = {"recorded": 0, "failed": 0, "skipped": 0}

    def record_call(self, record, **kwargs):
        if self._recorder is None:
            self.counts["skipped"] += 1
            return {"state": "skipped"}
        try:
            outcome = self._recorder.record_call(record, **kwargs)
        except Exception:
            self.counts["failed"] += 1
            raise
        self.counts["recorded" if outcome.get("state") == "recorded" else "skipped"] += 1
        return outcome


def _recorder(runtime_module, runtime, crm_store, args) -> _CountingRecorder:
    module = _load("runtime_file_calls_recorder", ROOT / "platform" / "channels" / "conversation_recorder.py")
    client = runtime.desired_state.get("source", {}).get("client")
    tenant = re.sub(r"[^a-z0-9-]+", "-", str(client or "").lower()).strip("-")[:63] or "default"  # as the worker names it
    agent = runtime.desired_state.get("agent", {}).get("metadata", {}).get("name")
    try:
        store = module._conversations().ConversationStore(
            args.conversations_root, tenant=tenant,
            audit=lambda action, by, details: runtime_module.GOVERN.record_operator_action(args.audit_path, action, by, details),
        )
    except ValueError:  # no tenant slug to scope the timeline to: record nothing rather than guess one
        return _CountingRecorder(None)
    return _CountingRecorder(module.ConversationRecorder(store, agent=agent, directory=module.contacts_directory(crm_store)))


def _summarizer(args):
    """A CallSummarizer using the coworker when one is configured, else the deterministic fallback (None with --no-summary)."""
    if args.no_summary:
        return None
    module = _load("runtime_file_calls_summary", ROOT / "platform" / "channels" / "call_summary.py")
    client = None
    key = os.environ.get("MODEL_GARDEN_COWORKER_API_KEY", "").strip()
    url = os.environ.get("MODEL_GARDEN_COWORKER_API_URL", "").strip()
    if not url and args.worker_config.is_file():
        try:
            url = str((json.loads(args.worker_config.read_text(encoding="utf-8")).get("coworker") or {}).get("url") or "")
        except (OSError, ValueError, AttributeError):
            url = ""
    if key and url:
        try:
            client = _load("runtime_file_calls_coworker", ROOT / "platform" / "runtime" / "coworker_client.py").CoworkerClient(url, key)
        except Exception:  # noqa: BLE001 - a bad coworker setting means the fallback, never a failed filing
            client = None
    return module.CallSummarizer(client, cache_path=args.conversations_root.with_name("call-summaries.json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transcripts", required=True, help="transcripts JSONL path, or - for standard input")
    parser.add_argument("--desired-state", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--audit-path", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")))
    parser.add_argument("--approval-root", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_APPROVAL_ROOT", "/opt/data/.modelgarden/approvals")))
    parser.add_argument("--conversations-root", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_CONVERSATIONS_ROOT", "/opt/data/.modelgarden/conversations")))
    parser.add_argument("--worker-config", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_WORKER_CONFIG", "/opt/profile-seed/.modelgarden/worker.json")))
    parser.add_argument("--no-summary", action="store_true", help="file the outcome-and-size line without a post-call summary")
    args = parser.parse_args(argv)
    try:
        runtime_module = _load("runtime_file_calls_governed_mcp", ROOT / "platform" / "runtime" / "governed_mcp.py")
        # A corrupt audit chain must stop the entire filing pass *before* a
        # governed CRM action, summary request or conversation write is attempted.
        # Do not turn an invalid ledger into an apparently successful filing.
        runtime_module.GOVERN.verify_audit_chain(args.audit_path)
        filing = _load("runtime_file_calls_filing", ROOT / "platform" / "channels" / "conversation_filing.py")
        runtime = runtime_module.GovernedToolRuntime(
            args.desired_state, args.audit_path, args.approval_root,
            environ={**os.environ, "MODEL_GARDEN_INITIATING_IDENTITY": FILING_IDENTITY},
        )
        store = runtime.crm_store()
        recorder = _recorder(runtime_module, runtime, store, args)
        if args.transcripts == "-":
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl") as handle:
                handle.write(sys.stdin.read())
                handle.flush()
                records = filing.read_call_transcripts(pathlib.Path(handle.name))
                write_failures = filing.read_transcript_failures(pathlib.Path(handle.name))
        else:
            records = filing.read_call_transcripts(pathlib.Path(args.transcripts))
            write_failures = filing.read_transcript_failures(pathlib.Path(args.transcripts))
        failures: list[str] = []
        summarizer = _summarizer(args)
        totals = filing.file_call_transcripts(runtime, store, records, log=failures.append, recorder=recorder, summarizer=summarizer)
        ledgered = filing.ledger_transcript_failures(runtime_module.GOVERN, args.audit_path, write_failures)
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        print(f"Runtime file-calls failed: {exc}", file=sys.stderr)
        return 1
    for failure in failures:
        print(f"failed to file {failure}", file=sys.stderr)
    print(json.dumps({"records": len(records), **totals, "writeFailures": ledgered, "timeline": recorder.counts,
                      "summaries": summarizer.counts if summarizer is not None else {}}, sort_keys=True))
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
