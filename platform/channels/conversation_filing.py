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
  record has one; never transcript text or message bodies. A record's *extractive* summary (the worker's
  model-free index, which quotes the first and last turns) is therefore not filed: it stays in the transcript store.
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
    summary = record.get("summary") if isinstance(record.get("summary"), str) and record["summary"].strip() and record.get("summarySource") != "extractive" else None
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


def read_voice_records(path: pathlib.Path) -> list[dict[str, Any]]:
    """Validate the *entire* bounded voice JSONL snapshot before any governed action.

    A valid prefix is not proof of a complete file. The voice writer publishes
    complete newline-terminated records atomically; reject partial lines,
    malformed records, duplicate call identities and oversized exports instead
    of silently dropping a customer call or a failed-write receipt. One read
    also avoids mixing two generations of an atomically replaced voice file.
    Error messages contain line numbers only, never caller content.
    """
    if not path.is_file():
        return []  # a never-provisioned voice store is an empty input, not live proof
    records: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            if len(records) >= MAX_RECORDS:
                raise ValueError("voice record limit exceeded; split and reconcile input before filing")
            if not raw.endswith("\n"):
                raise ValueError(f"incomplete voice record at line {number}")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError(f"malformed voice record at line {number}") from None
            if not isinstance(value, dict):
                raise ValueError(f"non-object voice record at line {number}")
            if value.get("type") == "CallTranscript":
                if (interaction_from_call(value) is None
                        or type(value.get("endedAt")) is not int
                        or value["endedAt"] < value["timestamp"]):
                    raise ValueError(f"invalid call transcript at line {number}")
                call_hash = value["callHash"]
                if call_hash in seen_calls:
                    raise ValueError(f"duplicate call transcript at line {number}")
                seen_calls.add(call_hash)
            elif value.get("type") == "TranscriptWriteFailed":
                if type(value.get("timestamp")) is not int:
                    raise ValueError(f"invalid transcript failure at line {number}")
            records.append(value)
    return records


def call_transcripts(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select transcripts from a previously validated snapshot, oldest first."""
    return sorted(
        (dict(row) for row in records if row.get("type") == "CallTranscript"),
        key=lambda item: (item["timestamp"], item["callHash"]),
    )


def read_call_transcripts(path: pathlib.Path) -> list[dict[str, Any]]:
    """Compatibility helper; callers needing both event types should read once."""
    return call_transcripts(read_voice_records(path))


_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
FAILURE_ACTION = "transcript.write-failed"


def transcript_failures(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select failure receipts from the same validated voice snapshot."""
    return sorted(
        (dict(row) for row in records if row.get("type") == "TranscriptWriteFailed"),
        key=lambda item: (item["timestamp"], str(item.get("roomHash", ""))),
    )


def read_transcript_failures(path: pathlib.Path) -> list[dict[str, Any]]:
    """Compatibility helper; callers needing calls too should read once."""
    return transcript_failures(read_voice_records(path))


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
    # Never treat a fingerprint from a broken local chain as an authoritative
    # receipt. In particular, valid JSON with a forged eventHash must not
    # suppress a failed transcript write or let us append a misleading receipt.
    # The shared ledger verifier checks every chained event, not just the tail.
    if audit_path is not None:
        try:
            govern.verify_audit_chain(audit_path)
        except ValueError:
            # The verifier may quote an untrusted schemaVersion; never surface
            # attacker-controlled ledger fields in operator or worker logs.
            raise ValueError("Audit chain invalid; operator reconciliation required") from None
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
    runtime: Any, store: Any, records: Iterable[Mapping[str, Any]], *, log: Callable[[str], None] | None = None, recorder: Any | None = None,
    summarizer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, int]:
    """File each call into the unmatched queue; a failure on one record never stops the rest.

    With a ``recorder`` (``conversation_recorder.ConversationRecorder``) each call is also put on the cross-channel conversation
    timeline. That is best-effort: a recording problem is logged by type and never changes how the call was filed.

    With a ``summarizer`` (``call_summary.CallSummarizer``) a call not yet filed first gets a post-call summary, model-written or the
    deterministic fallback, which is what ``crm.log-interaction`` carries (so an approval shows the exact text) and what the timeline
    records. Already-filed calls are not summarised again, so a re-run costs no model call. A summarizer error never blocks filing.
    """
    totals = {"filed": 0, "duplicate": 0, "pending-approval": 0, "failed": 0, "skipped": 0}
    for record in records:
        arguments = interaction_from_call(record)
        if arguments is None:
            totals["skipped"] += 1
            continue
        if summarizer is not None and not store.has_reference(arguments["transcriptRef"]):
            try:
                record = summarizer(record)
                arguments = interaction_from_call(record) or arguments
            except Exception as exc:  # noqa: BLE001 - optional summaries degrade, required completion evidence fails closed
                if log is not None:
                    log(f"{arguments['transcriptRef']}: summary {type(exc).__name__}")
                if getattr(summarizer, "requires_completion", False):
                    totals["failed"] += 1
                    continue  # no CRM/timeline action without the required completion receipt
        try:
            outcome = file_interaction(runtime, store, arguments)["status"]
        except Exception as exc:  # one bad record must not stop the others; report the type only
            outcome = "failed"
            if log is not None:
                log(f"{arguments['transcriptRef']}: {type(exc).__name__}")
        totals[outcome] += 1
        if recorder is not None:
            try:
                recorder.record_call(record)
            except Exception as exc:  # noqa: BLE001 - timeline problems never change filing
                if log is not None:
                    log(f"{arguments['transcriptRef']}: conversation timeline {type(exc).__name__}")
    return totals
