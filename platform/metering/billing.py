"""Billing sync: exported Action Ledger records to Lago usage events, per client, Agent and channel, plus a usage report.

Runs on the billing host (``infra/billing/gcp``) as the billing identity, which every client environment names as a ledger export
verifier, so it can read the retention-locked buckets and nothing else. The client runtimes hold no billing credential. Each pass:

1. lists the records each configured bucket gained since the last pass;
2. checks them before billing anything: every record's id is its event's chain hash, sequences are contiguous and each event links
   to the one before. A gap stops that source at the gap (the export is still writing) and a broken link fails the source;
3. turns ``usage.recorded`` facts (model tokens, from ``usage.py``) and succeeded governed ``execution`` events (Tool calls) into
   Lago events whose ``transaction_id`` derives from the ledger, so a retry is never billed twice;
4. appends the same rows to a local monthly journal (``report`` reads it), sends the events to Lago, and only then advances the
   source's cursor.

Lago owns plans, prices, allowances and invoices. The metrics here (``METRICS``) carry an ``agent`` filter, so a plan can price
or show each Agent separately; client and environment come from the source's subscription.
"""
from __future__ import annotations

import calendar
import contextlib
import datetime as dt
import fcntl
import importlib.util
import json
import os
import pathlib
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOKEN_KINDS = {"input": "inputTokens", "output": "outputTokens", "cache_read": "cacheReadTokens", "cache_write": "cacheWriteTokens"}
METRICS = (
    *({"code": f"llm_{kind}_tokens", "name": f"LLM {kind.replace('_', ' ')} tokens",
       "description": f"Model {kind.replace('_', ' ')} tokens used by an Agent, from the Hermes session counters."}
      for kind in TOKEN_KINDS),
    {"code": "tool_calls", "name": "Governed Tool calls", "description": "Governed Tool calls an Agent completed."},
)
REPORT_FIELDS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "apiCalls", "toolCalls",
                 "costMicrosUsd")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SUBSCRIPTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


class BillingError(RuntimeError):
    """A source cannot be billed safely; the message names the source and the problem, never record contents."""


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def ledger():
    return _load("model_garden_billing_ledger", "platform/core/ledger_export.py")


def lago():
    return _load("model_garden_billing_lago", "platform/metering/lago.py")


def metric_definitions(agents: Iterable[str]) -> list[dict[str, Any]]:
    """Every billable metric, summing ``quantity``; with an ``agent`` filter over the Agents billed, when there are any."""
    names = sorted(set(agents))
    return [
        {**metric, "aggregation_type": "sum_agg", "field_name": "quantity", "recurring": False,
         "filters": [{"key": "agent", "values": names}] if names else []}
        for metric in METRICS
    ]


def load_config(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the non-secret billing configuration. The Lago API key comes from the environment, never from here."""
    if not isinstance(document, Mapping) or set(document) - {"lago", "stateDir", "sources"}:
        raise BillingError("billing configuration holds lago, stateDir and sources only")
    settings = document.get("lago")
    if not isinstance(settings, Mapping) or set(settings) != {"url"}:
        raise BillingError("lago holds only url; the API key comes from LAGO_API_KEY")
    lago()._validated_http_url(str(settings["url"]), "lago.url")
    state_dir = document.get("stateDir")
    if not isinstance(state_dir, str) or not state_dir.startswith("/"):
        raise BillingError("stateDir must be an absolute path")
    sources = document.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BillingError("sources must list at least one client environment")
    seen = set()
    checked = []
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping) or set(source) - {"client", "environment", "subscription", "bucket", "prefix", "agents"}:
            raise BillingError(f"sources[{index}] holds client, environment, subscription, bucket, prefix and agents only")
        client, environment = str(source.get("client", "")), str(source.get("environment", ""))
        if not _SLUG.match(client) or not _SLUG.match(environment):
            raise BillingError(f"sources[{index}] needs a client and environment slug")
        if not _SUBSCRIPTION.match(str(source.get("subscription", ""))):
            raise BillingError(f"sources[{index}].subscription must be the Lago external subscription id")
        agents = source.get("agents") or []
        if not isinstance(agents, list) or not all(isinstance(agent, str) and _SLUG.match(agent) for agent in agents):
            raise BillingError(f"sources[{index}].agents must list Agent names")
        ledger().GcsSink(str(source.get("bucket", "")), lambda: "", prefix=str(source.get("prefix") or ""))  # validates the names
        key = f"{client}-{environment}"
        if key in seen:
            raise BillingError(f"{key} is listed twice")
        seen.add(key)
        checked.append({"client": client, "environment": environment, "subscription": str(source["subscription"]),
                        "bucket": str(source["bucket"]), "prefix": str(source.get("prefix") or ""), "agents": list(agents)})
    return {"lagoUrl": str(settings["url"]), "stateDir": pathlib.Path(state_dir), "sources": checked}


def _epoch(event: Mapping[str, Any]) -> int:
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    occurred = details.get("occurredAt")
    if isinstance(occurred, int) and not isinstance(occurred, bool) and occurred > 0:
        return occurred
    stamp = str(event.get("timestamp", "")).replace("Z", "+00:00")
    try:
        return calendar.timegm(dt.datetime.fromisoformat(stamp).utctimetuple())
    except ValueError as exc:
        raise BillingError("a ledger record has no usable timestamp") from exc


def billable_rows(records: Iterable[Mapping[str, Any]], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The usage rows a run of exported records carries: model usage facts and completed governed Tool calls."""
    rows = []
    for record in records:
        event = record["event"]
        details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
        common = {"client": source["client"], "environment": source["environment"], "subscription": source["subscription"],
                  "at": _epoch(event), "sequence": record["sequence"]}
        if event.get("event") == "operator-action" and event.get("action") == "usage.recorded":
            rows.append({
                **common, "id": str(details["usageId"]), "kind": "model", "agent": str(details.get("agent") or "unknown"),
                "channel": str(details.get("channel") or "unknown"), "surface": details.get("surface"),
                "provider": str(details.get("provider") or "unknown"), "model": details.get("model"),
                **{name: int(details.get(name) or 0) for name in REPORT_FIELDS if name != "toolCalls"}, "toolCalls": 0,
            })
        elif event.get("event") == "execution" and event.get("outcome") == "succeeded":
            context = event.get("ledger") if isinstance(event.get("ledger"), Mapping) else {}
            governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
            rows.append({
                **common, "id": str(record["recordId"]), "kind": "tool", "agent": str(event.get("agent") or "unknown"),
                "channel": str(governance.get("channel") or "unknown"), "provider": str(context.get("provider") or "built-in"),
                "tool": event.get("tool"), **{name: 0 for name in REPORT_FIELDS}, "toolCalls": 1,
            })
    return rows


def lago_events(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Lago usage events for the rows: one per token kind used and one per Tool call."""
    metering = lago()
    events = []
    for row in rows:
        extra = {"channel": row["channel"], **({"tool": row["tool"]} if row.get("tool") else {}),
                 **({"surface": row["surface"]} if row.get("surface") else {})}
        measures = ([(f"llm_{kind}_tokens", row[field], "tokens", f"{row['id']}:{kind}") for kind, field in TOKEN_KINDS.items()]
                    if row["kind"] == "model" else [("tool_calls", 1, "calls", f"{row['id']}:tool")])
        for code, quantity, unit, transaction in measures:
            if not quantity:
                continue
            properties = metering.build_attribution_properties(
                client=row["client"], agent=row["agent"], environment=row["environment"], provider=row["provider"],
                model=row.get("model") or None, usage_type=code, quantity=quantity, unit=unit,
                billing_subscription_id=row["subscription"], extra=extra,
            )
            events.append(metering.build_usage_event(
                transaction_id=transaction, external_subscription_id=row["subscription"], code=code, properties=properties,
                timestamp=row["at"],
            )["event"])
    return events


def checked_run(records: list[Mapping[str, Any]], cursor: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    """The contiguous, chain-verified records after ``cursor``; stops at a gap, raises on a broken or altered record.

    With no cursor yet the run starts at the first exported record, since legacy unchained records before the chain are never
    exported but still count in the sequence."""
    export = ledger()
    ordered = sorted(records, key=lambda item: item["sequence"])
    expected = int(cursor["sequence"]) + 1 if cursor else (ordered[0]["sequence"] if ordered else 1)
    previous = cursor.get("eventHash")
    run = []
    for record in ordered:
        if record["sequence"] < expected:
            continue
        if record["sequence"] != expected:
            break
        event = record.get("event")
        if not isinstance(event, Mapping) or event.get("sequence") != record["sequence"] or event.get("eventHash") != record.get("recordId") \
                or export.event_hash(event) != record.get("recordId"):
            raise BillingError(f"{name}: exported record {record['sequence']} does not match its chain hash")
        if previous is not None and event.get("previousEventHash") != previous:
            raise BillingError(f"{name}: exported record {record['sequence']} does not follow the record before it")
        run.append(record)
        expected, previous = expected + 1, record["recordId"]
    return run


@contextlib.contextmanager
def _locked(state_dir: pathlib.Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _cursor_path(state_dir: pathlib.Path, source: Mapping[str, Any]) -> pathlib.Path:
    return state_dir / "cursors" / f"{source['client']}-{source['environment']}.json"


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _journal(state_dir: pathlib.Path, rows: list[Mapping[str, Any]]) -> None:
    by_month: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_month.setdefault(dt.datetime.fromtimestamp(row["at"], dt.timezone.utc).strftime("%Y-%m"), []).append(row)
    for month, items in by_month.items():
        path = state_dir / "journal" / f"{month}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(json.dumps(item, sort_keys=True) + "\n" for item in items)
            handle.flush()
            os.fsync(handle.fileno())


def sync(config: Mapping[str, Any], *, sinks: Callable[[Mapping[str, Any]], Any], client: Any | None, dry_run: bool = False) -> dict[str, Any]:
    """One pass over every source. ``sinks(source)`` returns a readable export sink; ``client`` is a ``LagoClient``.

    A failing source is reported and does not stop the others; the pass raises at the end when any failed, so a scheduler sees it.
    """
    report: dict[str, Any] = {}
    failed = []
    with _locked(config["stateDir"]):
        for source in config["sources"]:
            name = f"{source['client']}-{source['environment']}"
            try:
                cursor_path = _cursor_path(config["stateDir"], source)
                cursor = _read_json(cursor_path)
                run = checked_run(sinks(source).records(after=int(cursor.get("sequence", 0))), cursor, name)
                rows = billable_rows(run, source)
                events = lago_events(rows)
                if dry_run or not run:
                    report[name] = {"records": len(run), "rows": len(rows), "events": len(events), **({"dryRun": True} if dry_run else {})}
                    continue
                _journal(config["stateDir"], rows)
                sent = client.send_events(events) if events else {"accepted": 0, "alreadyPresent": 0}
                _write_json(cursor_path, {"sequence": run[-1]["sequence"], "eventHash": run[-1]["recordId"]})
                report[name] = {"records": len(run), "rows": len(rows), **sent}
            except Exception as exc:  # noqa: BLE001 - one client never stops another; keep the type and our own message only
                failed.append(name)
                report[name] = {"failed": type(exc).__name__, **({"reason": str(exc)} if isinstance(exc, BillingError) else {})}
    if failed:
        raise BillingError(f"billing sync failed for {', '.join(failed)}: {json.dumps(report, sort_keys=True)}")
    return report


def usage_report(state_dir: pathlib.Path, *, since: str, until: str, by: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Usage per month, client, environment and Agent (plus any of channel, model, provider, tool) from the journal.

    ``since``/``until`` are ``YYYY-MM`` months, inclusive. A row journaled twice (a pass retried before its cursor advanced) is
    counted once.
    """
    for month in (since, until):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise BillingError("months are YYYY-MM")
    allowed = {"channel", "model", "provider", "tool"}
    if set(by) - allowed:
        raise BillingError(f"report can also group by {', '.join(sorted(allowed))}")
    keys = ("month", "client", "environment", "agent", *by)
    totals: dict[tuple, dict[str, Any]] = {}
    seen: set[str] = set()
    folder = state_dir / "journal"
    for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
        month = path.stem
        if not since <= month <= until:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            identity = f"{row['client']}-{row['environment']}:{row['id']}"
            if identity in seen:
                continue
            seen.add(identity)
            group = tuple([month, *(row.get(key) or "" for key in keys[1:])])
            entry = totals.setdefault(group, {**dict(zip(keys, group)), **{field: 0 for field in REPORT_FIELDS}})
            for field in REPORT_FIELDS:
                entry[field] += int(row.get(field) or 0)
    return [totals[group] for group in sorted(totals)]
