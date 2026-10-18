"""File an interaction through whichever CRM is bound, with delivery tracked in the provider-neutral sync ledger.

A finished call or email has to land in the client's CRM whichever CRM that is, survive outages and redelivery, and never be
reported as filed unless the provider said so (#108, #115). This module is the runner for that: it takes the portable
``crm.log-interaction`` arguments and an *executor* (the Tier 0 contact log or an approved upstream MCP executor, all with
the same ``arguments -> result`` shape), and records each attempt in ``crm_sync.SyncLedger``. It contains no provider code, is
credential-free. The runtime worker wires it to the support mailbox (``PayloadStore`` + the ``crm-sync`` job); the call path is still
the operator-invoked ``file-calls`` command.

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
  again, so ``retry_due`` takes a ``payloads`` callable; an entry whose payload is unavailable is skipped and counted, never invented.
- **A small payload store for retries.** ``PayloadStore`` keeps the portable ``crm.log-interaction`` arguments of an interaction that
  has not been filed yet (contact id, channel, direction, time, a one-line summary and a reference, never a message body) in a 0700
  directory next to the ledger, written atomically. It is passed to ``file_interaction`` so a failed attempt can be retried, and the
  copy is deleted as soon as the provider acknowledges the record. It is bounded and only holds what the ledger still has to deliver.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pathlib
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

MAX_PAYLOADS = 5000
MAX_PAYLOAD_BYTES = 8192
_KEY = re.compile(r"^[0-9a-f]{64}$")
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


class PayloadStore:
    """The arguments of interactions still waiting to be filed, by sync-ledger key (callable with a ledger entry, for ``retry_due``)."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _path(self, key: str) -> pathlib.Path:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("a payload is stored under a sync ledger key")
        return self.root / f"{key}.json"

    def put(self, key: str, arguments: Mapping[str, Any]) -> None:
        path = self._path(key)
        text = json.dumps(dict(arguments), sort_keys=True)
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("the interaction is too large to keep for a retry")
        if not path.exists() and len(self.keys()) >= MAX_PAYLOADS:
            raise ValueError("too many interactions are waiting to be filed")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        temporary = path.with_suffix(".tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def drop(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def keys(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json") if _KEY.fullmatch(path.stem)) if self.root.is_dir() else []

    def __call__(self, entry: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.get(entry["key"])


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
    store: PayloadStore | None = None,
) -> dict[str, Any]:
    """Attempt to file one interaction now, if its ledger entry allows an attempt; always returns where it stands.

    With a ``store`` the arguments are kept until the provider acknowledges the record, so a later ``retry_due`` can try again.
    """
    sync = _sync()
    entry = ledger.enqueue(channel=channel, external_id=external_id, interaction_type=interaction_type, now=now)
    key = entry["key"]
    if entry["state"] in ("synced", "reconciled"):
        if store is not None:
            store.drop(key)
        return _outcome(ledger, key, False, duplicate=True)
    if store is not None:
        store.put(key, arguments)  # kept even if this attempt is not allowed yet, so the retry job has something to send
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
        if store is not None:
            store.drop(key)
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
    store = payloads if isinstance(payloads, PayloadStore) else None
    for entry in ledger.due(now=now):
        arguments = payloads(entry)
        if arguments is None:
            counts["skipped"] += 1
            continue
        outcome = file_interaction(
            ledger, executor, arguments, channel=entry["channel"], external_id=entry["externalId"],
            interaction_type=entry["interactionType"], now=now, store=store,
        )
        counts["attempted"] += 1 if outcome["attempted"] else 0
        if outcome["state"] in ("synced", "reconciled"):
            counts["synced"] += 1
        elif outcome["state"] == "blocked":
            counts["blocked"] += 1
        else:
            counts["failed"] += 1
    return counts
