"""Attach ledger request ids to stored voice calls (#107, #115).

An authorization event explicitly tagged with ``governance.correlationId =
call:<callHash>`` is preferred even when calls overlap or ledger delivery is
delayed. A request id tagged to two different calls is ambiguous and omitted.

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
    if value is None or value == "":
        return None
    return value if isinstance(value, str) else False


def attach_request_ids(
    records: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]], *, identity: str = RUNTIME_IDENTITY
) -> list[dict[str, Any]]:
    """Attach exact call-correlated ids first; conservatively infer older untagged requests."""
    calls = [dict(record) for record in records]
    windows = [_window(call) for call in calls]
    exact_by_call: dict[str, list[tuple[float, str]]] = {}
    untagged: list[tuple[float, str]] = []
    request_tags: dict[str, set[str]] = {}
    for event in events:
        request_id, at = event.get("requestId"), _seconds(event.get("timestamp"))
        if event.get("event") != "authorization" or not isinstance(request_id, str) or not request_id or at is None:
            continue
        correlation = _event_correlation(event)
        if isinstance(correlation, str) and _CALL_CORRELATION.fullmatch(correlation):
            exact_by_call.setdefault(correlation, []).append((at, request_id))
            request_tags.setdefault(request_id, set()).add(correlation)
        elif correlation is None and event.get("initiatingUser") == identity:
            untagged.append((at, request_id))
        # Malformed or unrelated correlation never falls back to time guessing.

    for events_for_call in exact_by_call.values():
        events_for_call.sort()
    untagged.sort()
    for index, call in enumerate(calls):
        if call.get("requestIds"):
            continue
        call_hash = call.get("callHash")
        if isinstance(call_hash, str) and _CALL_HASH.fullmatch(call_hash):
            tag = "call:" + call_hash
            ids = list(dict.fromkeys(
                request_id for _, request_id in exact_by_call.get(tag, ())
                if request_tags[request_id] == {tag}
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
            if low <= at <= high and request_id not in request_tags
        ))[:MAX_REQUEST_IDS]
        if ids:
            call["requestIds"], call["requestIdsSource"] = ids, SOURCE
    return calls
