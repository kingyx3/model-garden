"""File finished conversations (calls, verified-sender emails) into the bound CRM through the governed boundary.

This is the "log-conversation" step that runs *after* a call or an email. It never reads customer
data back, never guesses whose conversation it was, and files through ``crm.log-interaction`` (and,
only when the client's policy lets it run, ``crm.create``) on the governed runtime, so every filing is
ledgered and follows the same provider binding as the Skill.

Matching rules (the privacy-critical part):

- A caller's number or name is a transport fact or an assertion, not a verified identity, so a call is
  always filed to the ``unmatched`` queue for a person to assign.
- An email is matched only when the sender was verified by the receiving server (see
  ``email_channel.py``): exactly one contact holding that verified address receives it. No contact,
  an ambiguous match or an unverified sender goes to ``unmatched``. A contact is created for a
  verified sender only when the deployment enabled it *and* ``crm.create`` actually executed (its
  Tool/policy may still require approval, in which case nothing is created).
- Filing is idempotent on the conversation reference: a reference already filed anywhere (including
  after a person assigned it) is skipped, so re-running after a retry, a restart or a webhook
  redelivery adds nothing.
- Summaries carry the outcome and size of the conversation, or the model-produced summary when the
  record has one; never transcript text or message bodies.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

UNMATCHED = "unmatched"
MAX_RECORDS = 5000


def _utc(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def interaction_from_call(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """``crm.log-interaction`` arguments (without ``objectId``) for one stored call transcript, or ``None``."""
    if record.get("type") != "CallTranscript":
        return None
    call_hash, started, outcome = record.get("callHash"), record.get("timestamp"), record.get("outcome")
    if not isinstance(call_hash, str) or not call_hash or not isinstance(started, int) or isinstance(started, bool):
        return None
    turns = record.get("turns") if isinstance(record.get("turns"), list) else []
    ended = record.get("endedAt") if isinstance(record.get("endedAt"), int) else started
    summary = record.get("summary") if isinstance(record.get("summary"), str) and record["summary"].strip() else None
    if summary is None:
        summary = f"Phone call, {max(0, ended - started)} seconds, {len(turns)} turns, outcome {outcome or 'unknown'}."
    arguments: dict[str, Any] = {
        "interactionType": "call", "channel": "voice", "direction": "inbound",
        "timestamp": _utc(started), "summary": summary, "transcriptRef": f"transcript:{call_hash}",
    }
    if isinstance(outcome, str) and outcome:
        arguments["outcome"] = outcome
    return arguments


def interaction_from_email(
    message_id: str, received_at: int, *, outcome: str | None = None, summary: str | None = None
) -> dict[str, Any]:
    """``crm.log-interaction`` arguments for one inbound email, referenced by a digest of its Message-ID."""
    reference = "email:" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    arguments: dict[str, Any] = {
        "interactionType": "email", "channel": "email", "direction": "inbound", "timestamp": _utc(received_at),
        "summary": summary or "Inbound email handled by the coworker.", "transcriptRef": reference,
    }
    if outcome:
        arguments["outcome"] = outcome
    return arguments


def _executed(result: Any) -> dict[str, Any] | None:
    if isinstance(result, Mapping) and result.get("status") == "executed" and isinstance(result.get("result"), Mapping):
        return dict(result["result"])
    return None


def file_interaction(
    runtime: Any,
    store: Any,
    arguments: Mapping[str, Any],
    *,
    verified_email: str | None = None,
    allow_create: bool = False,
) -> dict[str, Any]:
    """File one conversation; returns ``{"status": "filed"|"duplicate"|"pending-approval"|"failed", ...}``."""
    reference = arguments["transcriptRef"]
    if store.has_reference(reference):
        return {"status": "duplicate", "reference": reference}
    object_id = UNMATCHED
    basis = "unmatched"
    if verified_email:
        matches = store.find_by_email(verified_email)
        if len(matches) == 1:
            object_id, basis = matches[0], "verified-email"
        elif not matches and allow_create:
            created = _executed(runtime.crm_create(
                fields={"email": verified_email}, justification="first verified email from this sender",
            ))
            if created and isinstance(created.get("objectId"), str):
                object_id, basis = created["objectId"], "created-from-verified-email"
    result = runtime.crm_log_interaction(
        object_id=object_id, interaction_type=arguments["interactionType"], channel=arguments["channel"],
        direction=arguments["direction"], timestamp=arguments["timestamp"], summary=arguments["summary"],
        outcome=arguments.get("outcome"), transcript_ref=reference,
        justification="file finished conversation",
    )
    done = _executed(result)
    if done is None:
        status = result.get("status") if isinstance(result, Mapping) else None
        return {"status": "pending-approval" if status == "pending-approval" else "failed", "reference": reference}
    return {
        "status": "duplicate" if done.get("duplicate") else "filed", "reference": reference, "basis": basis,
        "externalObjectRef": done.get("externalObjectRef"),
    }


def read_call_transcripts(path: pathlib.Path) -> list[dict[str, Any]]:
    """Stored call transcript records, oldest first; unreadable lines are skipped."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("type") == "CallTranscript":
                records.append(value)
            if len(records) >= MAX_RECORDS:
                break
    return sorted(records, key=lambda item: (item.get("timestamp", 0), item.get("callHash", "")))


_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
FAILURE_ACTION = "transcript.write-failed"


def read_transcript_failures(path: pathlib.Path) -> list[dict[str, Any]]:
    """The voice worker's ``TranscriptWriteFailed`` lifecycle events (error type and time only), oldest first."""
    if not path.is_file():
        return []
    failures: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("type") == "TranscriptWriteFailed" and isinstance(value.get("timestamp"), int):
                failures.append(value)
            if len(failures) >= MAX_RECORDS:
                break
    return sorted(failures, key=lambda item: (item["timestamp"], str(item.get("roomHash", ""))))


def _failure_fingerprint(failure: Mapping[str, Any]) -> str:
    material = f"{failure['timestamp']}\0{failure.get('roomHash', '')}\0{failure.get('errorType', '')}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _known_fingerprints(audit_path: pathlib.Path | None) -> set[str]:
    known: set[str] = set()
    if audit_path is None or not audit_path.is_file():
        return known
    with audit_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("action") == FAILURE_ACTION:
                fingerprint = (event.get("details") or {}).get("fingerprint")
                if isinstance(fingerprint, str):
                    known.add(fingerprint)
    return known


def ledger_transcript_failures(
    govern: Any, audit_path: pathlib.Path | None, failures: Iterable[Mapping[str, Any]], *, performed_by: str = "runtime:conversation-filing"
) -> dict[str, int]:
    """Ledger each transcript write failure once (error type and time only; no transcript text exists to leak).

    A failure the ledger already holds, recognised by a fingerprint of its time, call hash and error type, is not
    recorded again, so the step can run repeatedly over the same worker log.
    """
    known = _known_fingerprints(audit_path)
    totals = {"ledgered": 0, "known": 0}
    for failure in failures:
        fingerprint = _failure_fingerprint(failure)
        if fingerprint in known:
            totals["known"] += 1
            continue
        error_type = failure.get("errorType")
        details: dict[str, Any] = {
            "fingerprint": fingerprint, "at": failure["timestamp"],
            "errorType": error_type if isinstance(error_type, str) and _ERROR_TYPE.match(error_type) else "Unknown",
        }
        if isinstance(failure.get("roomHash"), str) and re.fullmatch(r"[0-9a-f]{64}", failure["roomHash"]):
            details["callHash"] = failure["roomHash"]
        govern.record_operator_action(audit_path, FAILURE_ACTION, performed_by, details)
        known.add(fingerprint)
        totals["ledgered"] += 1
    return totals


def file_call_transcripts(
    runtime: Any, store: Any, records: Iterable[Mapping[str, Any]], *, log: Callable[[str], None] | None = None
) -> dict[str, int]:
    """File each call into the unmatched queue; a failure on one record never stops the rest."""
    totals = {"filed": 0, "duplicate": 0, "pending-approval": 0, "failed": 0, "skipped": 0}
    for record in records:
        arguments = interaction_from_call(record)
        if arguments is None:
            totals["skipped"] += 1
            continue
        try:
            outcome = file_interaction(runtime, store, arguments)["status"]
        except Exception as exc:  # one bad record must not stop the others; report the type only
            outcome = "failed"
            if log is not None:
                log(f"{arguments['transcriptRef']}: {type(exc).__name__}")
        totals[outcome] += 1
    return totals
