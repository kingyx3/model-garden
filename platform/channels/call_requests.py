"""Attach ledger request ids to stored voice calls (#107, #115).

An authorization event explicitly tagged with ``governance.correlationId =
call:<callHash>`` is preferred even when calls overlap or ledger delivery is
delayed. A request id seen with conflicting tags, an untagged duplicate or
malformed task/correlation context or conflicting acting identities are
ambiguous and omitted, even if a conflicting event has an invalid timestamp.

For older untagged runtime events, retain the labelled time-window heuristic
only when the stored call windows do not overlap. Never infer attribution from
events already correlated to a different task or call. This is NOT proof that
the voice runtime emits call correlation tags; live call evidence is separate.

Pure Python over ledger events; no credentials or I/O.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from typing import Any

SOURCE = "ledger-time-window"
EXACT_SOURCE = "ledger-correlation"
RUNTIME_IDENTITY = "runtime:hermes"
GRACE_SECONDS = 5
MAX_REQUEST_IDS = 200
_CALL_HASH = re.compile(r"[0-9a-f]{64}")
_CALL_CORRELATION = re.compile(r"call:[0-9a-f]{64}")


def _seconds(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None  # local-time interpretation would make attribution host-dependent
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _window(record: Mapping[str, Any]) -> tuple[int, int] | None:
    start = record.get("timestamp")
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    end = record.get("endedAt")
    return start, end if isinstance(end, int) and not isinstance(end, bool) and end >= start else start


def _event_correlation(event: Mapping[str, Any]) -> str | None | bool:
    """None is untagged; False is malformed; a string is an explicit tag."""
    governance = event.get("governance")
    if governance is None:
        return None
    if not isinstance(governance, Mapping):
        return False
    value = governance.get("correlationId")
    if "correlationId" in governance:
        # An explicitly empty/malformed tag is not equivalent to no tag.
        return value if isinstance(value, str) and value else False
    # A request already bound to a different task cannot be guessed into a
    # voice call solely because the authorization happened during that call.
    if "taskId" in governance or "parentRequestId" in governance:
        return False
    return None


def attach_request_ids(
    records: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]], *, identity: str = RUNTIME_IDENTITY
) -> list[dict[str, Any]]:
    """Recompute IDs from ledger authorizations, preferring exact call tags over legacy time inference."""
    calls = [dict(record) for record in records]
    windows = [_window(call) for call in calls]
    exact_by_call: dict[str, list[tuple[float, str, str]]] = {}
    untagged: list[tuple[float, str]] = []
    # Track every authorization for an ID, including malformed timestamps.
    # A reused ID with conflicting task/call tags must never be credited to
    # whichever call happened to be in progress.
    request_states: dict[str, set[tuple[str, str, str]]] = {}
    for event in events:
        request_id = event.get("requestId")
        if event.get("event") != "authorization" or not isinstance(request_id, str) or not request_id:
            continue
        correlation = _event_correlation(event)
        actor = event.get("initiatingUser")
        # A request ID is only unambiguous when every authorization agrees
        # on BOTH its correlation and acting identity. An absent actor is not
        # proof of a caller's governed action, even with an exact call tag.
        valid_actor = isinstance(actor, str) and bool(actor) and actor == actor.strip()
        state = ("invalid", "", "") if not valid_actor or correlation is False else (
            ("untagged", "", actor) if correlation is None else ("tag", correlation, actor)
        )
        request_states.setdefault(request_id, set()).add(state)
        at = _seconds(event.get("timestamp"))
        if at is None or not valid_actor:
            continue
        if isinstance(correlation, str) and _CALL_CORRELATION.fullmatch(correlation):
            exact_by_call.setdefault(correlation, []).append((at, request_id, actor))
        elif correlation is None and actor == identity:
            untagged.append((at, request_id))
        # Malformed or unrelated correlation never falls back to time guessing.

    for events_for_call in exact_by_call.values():
        events_for_call.sort()
    untagged.sort()
    for index, call in enumerate(calls):
        # The voice JSONL is an input to reconciliation, not an authority for
        # governed actions. Never let prefilled request IDs or a claimed source
        # bypass the current ledger's actor/correlation conflict checks.
        # A previously enriched snapshot is safe to reconcile repeatedly.
        call.pop("requestIds", None)
        call.pop("requestIdsSource", None)
        call_hash = call.get("callHash")
        if isinstance(call_hash, str) and _CALL_HASH.fullmatch(call_hash):
            tag = "call:" + call_hash
            ids = list(dict.fromkeys(
                request_id for _, request_id, actor in exact_by_call.get(tag, ())
                if request_states[request_id] == {("tag", tag, actor)}
            ))[:MAX_REQUEST_IDS]
            if ids:
                call["requestIds"], call["requestIdsSource"] = ids, EXACT_SOURCE
                continue

        window = windows[index]
        if window is None:
            continue
        low, high = window[0] - GRACE_SECONDS, window[1] + GRACE_SECONDS
        if any(other is not None and other[0] - GRACE_SECONDS <= high and low <= other[1] + GRACE_SECONDS
               for position, other in enumerate(windows) if position != index):
            continue
        ids = list(dict.fromkeys(
            request_id for at, request_id in untagged
            if low <= at <= high and request_states[request_id] == {("untagged", "", identity)}
        ))[:MAX_REQUEST_IDS]
        if ids:
            call["requestIds"], call["requestIdsSource"] = ids, SOURCE
    return calls
