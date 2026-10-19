"""Ledger request ids for a finished voice call (#107, #115).

The voice container cannot see the Action Ledger, so a stored call carries no ``requestIds``. This step runs where the ledger is
readable (the worker) and attaches the ids of the governed Tool requests the coworker itself started while the call was live, so one
call reads as one story on the conversation timeline and in ``client-operator.py audit``.

The attribution is by time, not by a tag, and is labelled that way (``requestIdsSource: ledger-time-window``):

- only authorization events whose initiating identity is the runtime's own (a Tool call made on behalf of a named person, a channel
  principal or a scheduled task carries that person's identity instead) inside the call's window, with a few seconds of grace;
- a call whose window overlaps another stored call gets no ids, because the ledger cannot say which call a request belonged to;
- ids a record already carries are never replaced, and nothing is written back to the transcript store.

Pure Python over a list of ledger events; no credentials, no I/O.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

SOURCE = "ledger-time-window"
RUNTIME_IDENTITY = "runtime:hermes"
GRACE_SECONDS = 5
MAX_REQUEST_IDS = 200


def _seconds(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(record: Mapping[str, Any]) -> tuple[int, int] | None:
    start = record.get("timestamp")
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    end = record.get("endedAt")
    return start, end if isinstance(end, int) and not isinstance(end, bool) and end >= start else start


def attach_request_ids(
    records: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]], *, identity: str = RUNTIME_IDENTITY
) -> list[dict[str, Any]]:
    """Each record with ``requestIds`` filled from the ledger when it has none and its window is unambiguous."""
    calls = [dict(record) for record in records]
    windows = [_window(call) for call in calls]
    requests: list[tuple[float, str]] = []
    for event in events:
        request_id, at = event.get("requestId"), _seconds(event.get("timestamp"))
        if event.get("event") == "authorization" and event.get("initiatingUser") == identity and isinstance(request_id, str) and request_id and at is not None:
            requests.append((at, request_id))
    requests.sort()
    for index, call in enumerate(calls):
        window = windows[index]
        if call.get("requestIds") or window is None:
            continue
        low, high = window[0] - GRACE_SECONDS, window[1] + GRACE_SECONDS
        if any(other is not None and other[0] - GRACE_SECONDS <= high and low <= other[1] + GRACE_SECONDS
               for position, other in enumerate(windows) if position != index):
            continue
        ids = list(dict.fromkeys(request_id for at, request_id in requests if low <= at <= high))[:MAX_REQUEST_IDS]
        if ids:
            call["requestIds"], call["requestIdsSource"] = ids, SOURCE
    return calls
