"""File an interaction through whichever CRM is bound, with delivery tracked in the provider-neutral sync ledger.

A finished call or email has to land in the client's CRM whichever CRM that is, survive outages and redelivery, and never be
reported as filed unless the provider said so (#108, #115). This module is the runner for that: it takes the portable
``crm.log-interaction`` arguments and an *executor* (the Tier 0 contact log or an approved upstream MCP executor, all with
the same ``arguments -> result`` shape), and records each attempt in ``crm_sync.SyncLedger``. It contains no provider code, is
credential-free and is **not yet wired** to the voice or mail paths.

- **One entry per call or message.** The ledger key comes from the channel, the call or message id and the interaction type, so
  redelivery, a retry and a restart all reach the same entry, and nothing is filed twice.
- **Success only on the provider's word.** A result without an ``externalObjectRef`` is a rejection, not a success. A result the
  provider flags ``duplicate`` means the record was already there (for example the first attempt landed but its answer was lost),
  so the entry is *reconciled* to the existing reference instead of being written again.
- **Failures are sorted, not guessed.** An error carrying a ``code`` from the sync vocabulary (the HubSpot adapter's
  ``HubSpotError``) keeps it; a ``PermissionError`` is ``unauthorized``; a ``ValueError`` (bad arguments, unknown contact) is
  ``rejected`` and waits for a person; anything else (timeouts, network errors) is a transient ``outage`` that backs off and, after
  the ceiling, blocks.
- **No content in the ledger.** The sync ledger holds identifiers, states and error codes only. Retrying needs the interaction
  again, so ``retry_due`` takes a ``payloads`` callable (a deployment would read it from its transcript or message store); an entry
  whose payload is unavailable is skipped and counted, never invented.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from collections.abc import Callable, Mapping
from typing import Any

Executor = Callable[[dict[str, Any]], Mapping[str, Any]]


def _sync():
    name = "model_garden_crm_filing_sync"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("crm_sync.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def classify(exc: BaseException) -> str:
    """The sync error code for a failure from an executor."""
    sync = _sync()
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in sync.ERROR_CODES:
        return code
    if isinstance(exc, PermissionError):
        return "unauthorized"
    if isinstance(exc, ValueError):
        return "rejected"
    return "outage"


def _outcome(ledger: Any, key: str, attempted: bool, **extra: Any) -> dict[str, Any]:
    entry = ledger.get(key)
    shown = _sync().describe(entry)
    return {"key": key, "state": entry["state"], "attempts": entry["attempts"], "attempted": attempted,
            "providerRef": entry.get("providerRef"), "repair": shown["repair"], "message": shown["message"], **extra}


def file_interaction(
    ledger: Any,
    executor: Executor,
    arguments: Mapping[str, Any],
    *,
    channel: str,
    external_id: str,
    interaction_type: str,
    now: int,
) -> dict[str, Any]:
    """Attempt to file one interaction now, if its ledger entry allows an attempt; always returns where it stands."""
    sync = _sync()
    entry = ledger.enqueue(channel=channel, external_id=external_id, interaction_type=interaction_type, now=now)
    key = entry["key"]
    if entry["state"] in ("synced", "reconciled"):
        return _outcome(ledger, key, False, duplicate=True)
    if entry["state"] == "blocked" or entry["nextAttemptAt"] > now:
        return _outcome(ledger, key, False, duplicate=False)
    try:
        result = executor(dict(arguments))
    except Exception as exc:  # noqa: BLE001 - every executor failure is sorted into a sync code
        ledger.record_failure(key, error_code=classify(exc), now=now)
        return _outcome(ledger, key, True, duplicate=False)
    reference = result.get("externalObjectRef") if isinstance(result, Mapping) else None
    if not isinstance(reference, str) or not reference:
        ledger.record_failure(key, error_code="rejected", now=now)  # no acknowledgement, no success
        return _outcome(ledger, key, True, duplicate=False)
    try:
        if result.get("duplicate") is True:
            ledger.reconcile(key, provider_ref=reference, now=now)
        else:
            ledger.record_success(key, provider_ref=reference, now=now)
    except sync.SyncError:
        ledger.record_failure(key, error_code="rejected", now=now)  # an unusable reference is a rejection
        return _outcome(ledger, key, True, duplicate=False)
    return _outcome(ledger, key, True, duplicate=bool(result.get("duplicate")))


def retry_due(
    ledger: Any,
    executor: Executor,
    payloads: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    *,
    now: int,
) -> dict[str, int]:
    """Attempt every entry whose backoff has passed, taking each interaction from ``payloads(entry)``."""
    counts = {"attempted": 0, "synced": 0, "failed": 0, "blocked": 0, "skipped": 0}
    for entry in ledger.due(now=now):
        arguments = payloads(entry)
        if arguments is None:
            counts["skipped"] += 1
            continue
        outcome = file_interaction(
            ledger, executor, arguments, channel=entry["channel"], external_id=entry["externalId"],
            interaction_type=entry["interactionType"], now=now,
        )
        counts["attempted"] += 1 if outcome["attempted"] else 0
        if outcome["state"] in ("synced", "reconciled"):
            counts["synced"] += 1
        elif outcome["state"] == "blocked":
            counts["blocked"] += 1
        else:
            counts["failed"] += 1
    return counts
