"""Meter model usage from the Hermes session stores into the Action Ledger, per client, Agent, channel and model.

Hermes keeps cumulative token and cost counters for every conversation session in its own ``state.db`` (SQLite, WAL). The
runtime worker reads each store read-only on a timer, turns what changed since the last pass into one usage fact per session, and
the worker ledgers each fact as ``usage.recorded``. The ledger export then carries the facts, hash-chained, to the client's
retention-locked bucket, where the billing sync (``billing.py``) reads them as the verifier identity and sends them to Lago. The
runtime therefore holds no billing credential and sends nothing anywhere itself.

Properties:
- **Exact deltas.** A fact is the difference between a session's counters now and the counters already metered, so usage is never
  counted twice and a pass that is skipped loses nothing: the next pass picks up the whole difference.
- **Idempotent identity.** ``usageId`` is a hash of the store, the session and the cumulative counters the fact ends at. Recording
  the same fact again (a crash between the ledger write and ``commit``) yields the same ``usageId``, which the billing sync and Lago
  use as the transaction id, so a retry is never billed twice.
- **Counts only.** A fact carries counters, the model and provider names, the channel Hermes reports (``source``) and a short hash
  of the session id. Never a message, prompt, title, user id or chat id.
- **Read-only.** Stores are opened with ``mode=ro``; the worker mounts them read-only. A store that is missing or cannot be opened
  (Hermes not started yet) is reported and retried next pass.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import pathlib
import re
import sqlite3
from collections.abc import Iterator, Mapping
from typing import Any

COUNTERS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "apiCalls", "costMicrosUsd")
_COLUMNS = {
    "inputTokens": "input_tokens", "outputTokens": "output_tokens", "cacheReadTokens": "cache_read_tokens",
    "cacheWriteTokens": "cache_write_tokens", "reasoningTokens": "reasoning_tokens", "apiCalls": "api_call_count",
}
_REQUIRED = ("id", "source", "input_tokens", "output_tokens")
_OPTIONAL = ("model", "billing_provider", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_call_count",
             "estimated_cost_usd", "actual_cost_usd", "last_activity_at")
_SURFACE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LABEL = re.compile(r"[^A-Za-z0-9._:/@+-]+")


class UsageError(RuntimeError):
    """A usage store is unreadable or does not look like a Hermes session store."""


def _label(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _LABEL.sub("-", value.strip())[:128]


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0
    return int(value)


def parse_sources(value: str) -> dict[str, pathlib.Path]:
    """``name=/path/state.db,name=/path/state.db`` (``MODEL_GARDEN_USAGE_SOURCES``) as a map of store name to path."""
    sources: dict[str, pathlib.Path] = {}
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        name, separator, path = item.partition("=")
        if not separator or not _SURFACE.match(name.strip()) or not path.strip().startswith("/"):
            raise ValueError("usage sources are name=/absolute/path/state.db pairs separated by commas")
        if name.strip() in sources:
            raise ValueError(f"usage source {name.strip()!r} is listed twice")
        sources[name.strip()] = pathlib.Path(path.strip())
    return sources


@contextlib.contextmanager
def _read_only(path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise UsageError(f"usage store could not be opened: {type(exc).__name__}") from exc
    try:
        yield connection
    finally:
        connection.close()


def read_sessions(path: pathlib.Path) -> list[dict[str, Any]]:
    """Every session's cumulative counters from a Hermes ``state.db``, keyed by our counter names."""
    try:
        with _read_only(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            missing = [name for name in _REQUIRED if name not in columns]
            if missing:
                raise UsageError("usage store has no Hermes sessions table with token counters")
            selected = [*_REQUIRED, *(name for name in _OPTIONAL if name in columns)]
            rows = connection.execute(f"SELECT {', '.join(selected)} FROM sessions").fetchall()  # nosec B608 - fixed column names
    except sqlite3.Error as exc:
        raise UsageError(f"usage store could not be read: {type(exc).__name__}") from exc
    sessions = []
    for row in rows:
        values = dict(zip(selected, row))
        cost, cost_source = None, None
        for column, source in (("actual_cost_usd", "actual"), ("estimated_cost_usd", "estimated")):
            value = values.get(column)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                cost, cost_source = value, source
                break
        sessions.append({
            "id": str(values["id"]),
            "channel": _label(values.get("source")) or "unknown",
            "model": _label(values.get("model")),
            "provider": _label(values.get("billing_provider")),
            "costSource": cost_source,
            "lastActivityAt": _count(values.get("last_activity_at")) or None,
            **{name: _count(values.get(column)) for name, column in _COLUMNS.items()},
            "costMicrosUsd": _count(round(cost * 1_000_000)) if cost is not None else 0,
        })
    return sessions


def _usage_id(surface: str, session: str, totals: Mapping[str, int]) -> str:
    material = json.dumps({"surface": surface, "session": session, "totals": dict(totals)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class UsageMeter:
    """Turns cumulative session counters into deltas; ``collect`` is pure, ``commit`` advances the cursor once facts are ledgered.

    The cursor is a small SQLite file holding the last metered counters of every session, so a resumed session long after it
    went quiet is still metered from where it stopped rather than from zero.
    """

    def __init__(self, cursor_path: pathlib.Path) -> None:
        self.path = pathlib.Path(cursor_path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metered (surface TEXT NOT NULL, session TEXT NOT NULL, totals TEXT NOT NULL, "
                "PRIMARY KEY (surface, session))"
            )
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _metered(self, surface: str) -> dict[str, dict[str, int]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT session, totals FROM metered WHERE surface = ?", (surface,)).fetchall()
        return {session: json.loads(totals) for session, totals in rows}

    def collect(self, surface: str, sessions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """One fact per session whose counters grew since the last commit. A counter that went down (a store that was reset)
        starts a new baseline for that counter instead of producing a negative or a full re-count."""
        if not _SURFACE.match(surface):
            raise ValueError("a usage source name is lowercase letters, digits and hyphens")
        metered = self._metered(surface)
        facts = []
        for session in sessions:
            totals = {name: _count(session.get(name)) for name in COUNTERS}
            before = metered.get(str(session["id"]), {})
            delta = {name: max(0, totals[name] - _count(before.get(name))) for name in COUNTERS}
            if not any(delta.values()):
                if totals != {name: _count(before.get(name)) for name in COUNTERS}:
                    facts.append({"surface": surface, "sessionId": str(session["id"]), "totals": totals, "baselineOnly": True})
                continue
            facts.append({
                "surface": surface, "sessionId": str(session["id"]), "totals": totals,
                "usageId": _usage_id(surface, str(session["id"]), totals),
                "channel": session.get("channel") or "unknown",
                **({"model": session["model"]} if session.get("model") else {}),
                **({"provider": session["provider"]} if session.get("provider") else {}),
                **({"costSource": session["costSource"]} if session.get("costSource") and delta["costMicrosUsd"] else {}),
                **({"occurredAt": session["lastActivityAt"]} if session.get("lastActivityAt") else {}),
                **delta,
            })
        return facts

    def commit(self, facts: list[Mapping[str, Any]]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO metered (surface, session, totals) VALUES (?, ?, ?) "
                "ON CONFLICT(surface, session) DO UPDATE SET totals = excluded.totals",
                [(fact["surface"], fact["sessionId"], json.dumps(fact["totals"], sort_keys=True)) for fact in facts],
            )


def ledger_details(fact: Mapping[str, Any], *, agent: str) -> dict[str, Any]:
    """The ``usage.recorded`` ledger details for a fact: counters and names only, the session as a short hash.

    ``usage`` repeats the total tokens and cost in the shape the audit timeline already sums (``audit_query``)."""
    return {
        "usageId": fact["usageId"],
        "agent": agent,
        "surface": fact["surface"],
        "session": hashlib.sha256(str(fact["sessionId"]).encode("utf-8")).hexdigest()[:16],
        "channel": fact["channel"],
        **{key: fact[key] for key in ("model", "provider", "costSource", "occurredAt") if key in fact},
        **{name: fact[name] for name in COUNTERS},
        "usage": {"tokens": fact["inputTokens"] + fact["outputTokens"], "costCents": round(fact["costMicrosUsd"] / 10_000)},
    }
