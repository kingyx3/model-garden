#!/usr/bin/env python3
"""Collect redacted, machine-readable evidence from a running reference deployment.

With --require-two-way-transcript, read the private default voice transcript
file and require a structurally complete disclosed caller/agent exchange
matched to a completed call. This does not prove audible SIP delivery, and a
custom transcript path requires independent operator review.

The collector never copies prompts, Tool arguments, caller numbers, message text, or
credentials into the evidence pack. It records service health and the existence/types
of governance and voice lifecycle records. Run it after deployment and again after the
real inbound-call/business-action acceptance scenario.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import pathlib
import re
import subprocess
import sys
from functools import lru_cache
from collections import Counter
from typing import Any

PROJECT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _containers(project: str) -> dict[str, dict[str, str]]:
    result = _run([
        "docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
        "--format", '{{.Label "com.docker.compose.service"}}|{{.Status}}|{{.ID}}',
    ])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "cannot inspect Docker services")
    services: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("malformed reference runtime service inventory")
        if parts[0] in services:
            raise ValueError("duplicate reference runtime service entry")
        services[parts[0]] = {"status": parts[1], "container": parts[2]}
    return services


def _jsonl(container: str, path: str) -> list[dict[str, Any]]:
    # A missing/unreadable or partially written evidence file is NOT an empty
    # successful read. In particular, never suppress a failed 'cat' with '|| true'.
    result = _run(["docker", "exec", container, "sh", "-c", f"test -f {path} && cat {path}"])
    if result.returncode != 0:
        raise ValueError("cannot read reference evidence file")
    if result.stdout and not result.stdout.endswith("\n"):
        raise ValueError("incomplete reference evidence record")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(result.stdout.splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid reference evidence record at line {number}") from None
        if not isinstance(value, dict):
            raise ValueError(f"non-object reference evidence record at line {number}")
        rows.append(value)
    return rows


def _audit_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    approvals = 0
    head_hash = None
    for row in rows:
        status = row.get("status") or row.get("decision") or row.get("result") or row.get("outcome")
        if isinstance(status, str):
            statuses[status] += 1
        tool = row.get("tool") or row.get("toolName") or row.get("action")
        if isinstance(tool, str):
            tools[tool] += 1
        details = row.get("details")
        if row.get("approval") or row.get("approver") or row.get("approvalReference") or (isinstance(details, dict) and details.get("approver")):
            approvals += 1
        event_hash = row.get("eventHash")
        if isinstance(event_hash, str) and event_hash:
            head_hash = event_hash
    return {
        "records": len(rows),
        "statuses": dict(sorted(statuses.items())),
        "tools": dict(sorted(tools.items())),
        "approvalEvidenceRecords": approvals,
        "successfulActions": sum(
            row.get("event") == "execution" and row.get("outcome") == "succeeded"
            for row in rows
        ),
        "auditHeadHash": head_hash,
    }


def _voice_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    types = Counter(row.get("type") for row in rows if isinstance(row.get("type"), str))
    call_ids = {
        row.get("roomHash") or row.get("room")
        for row in rows
        if row.get("type") == "CallStarted" and isinstance(row.get("roomHash") or row.get("room"), str)
    }
    # A start alone is not a completed call. Match lifecycle events to the
    # same room AND Agent with ordered integer timestamps; repeated records
    # must not inflate the count. These receipts are still not SIP attestation.
    started: dict[tuple[str, str], int] = {}
    for row in rows:
        room, agent, timestamp = row.get("roomHash"), row.get("agent"), row.get("timestamp")
        if (row.get("type") == "CallStarted" and isinstance(room, str) and room
                and isinstance(agent, str) and agent and type(timestamp) is int):
            key = (room, agent)
            started[key] = min(timestamp, started.get(key, timestamp))
    completed: dict[str, tuple[int, int, str]] = {}
    for row in rows:
        room, agent, timestamp = row.get("roomHash"), row.get("agent"), row.get("timestamp")
        if (row.get("type") == "CallEnded" and isinstance(room, str)
                and isinstance(agent, str) and type(timestamp) is int
                and (room, agent) in started and timestamp >= started[(room, agent)]):
            beginning = started[(room, agent)]
            if room not in completed:
                completed[room] = (beginning, timestamp, agent)
    fallback_rooms = {
        row["roomHash"]
        for row in rows
        if row.get("type") in ("HumanTransfer", "FallbackMessage")
        and isinstance(row.get("roomHash"), str)
        and row["roomHash"] in completed
        and row.get("agent") == completed[row["roomHash"]][2]
        and type(row.get("timestamp")) is int
        and completed[row["roomHash"]][0] <= row["timestamp"] <= completed[row["roomHash"]][1]
    }
    return {
        "records": len(rows),
        "eventTypes": dict(sorted(types.items())),
        "distinctCalls": len(call_ids),
        "completedCalls": len(completed),
        "completedCallFallbackEvidence": len(fallback_rooms),
        "transcriptWriteFailures": types.get("TranscriptWriteFailed", 0),
        "humanTransferEvidence": types.get("HumanTransfer", 0),
        "messageFallbackEvidence": types.get("FallbackMessage", 0),
    }


def _completed_call_actions(voice_rows: list[dict[str, Any]], audit_rows: list[dict[str, Any]]) -> int:
    """Count uniquely authorized successful Tool requests explicitly tied to completed calls.

    Structural correlation only, NOT trusted SIP/identity/provider attestation.
    Untagged time-window guesses, unrelated agents and conflicting reused request IDs
    cannot turn a background Tool execution into reference-call acceptance.
    Only aggregate counts leave the collector; call/request/actor IDs stay private.
    """
    started: dict[tuple[str, str], int] = {}
    for row in voice_rows:
        room, agent, at = row.get("roomHash"), row.get("agent"), row.get("timestamp")
        if (row.get("type") == "CallStarted" and isinstance(room, str)
                and re.fullmatch(r"[0-9a-f]{64}", room) and isinstance(agent, str) and agent
                and type(at) is int):
            key = (room, agent)
            started[key] = min(at, started.get(key, at))
    completed: set[tuple[str, str]] = set()
    for row in voice_rows:
        room, agent, at = row.get("roomHash"), row.get("agent"), row.get("timestamp")
        if (row.get("type") == "CallEnded" and type(at) is int
                and isinstance(room, str) and isinstance(agent, str)
                and (room, agent) in started and at >= started[(room, agent)]):
            completed.add((room, agent))

    requests: dict[str, dict[str, Any]] = {}
    for row in audit_rows:
        request_id = row.get("requestId")
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{64}", request_id):
            continue
        context = row.get("governance")
        tag = context.get("correlationId") if isinstance(context, dict) else None
        # Include malformed or missing fields as conflicting signatures. Never
        # silently ignore a second record with the same request ID.
        signature = tuple(
            value if isinstance(value, str) and value else None
            for value in (row.get("agent"), row.get("initiatingUser"), tag, row.get("tool"))
        )
        request = requests.setdefault(request_id, {
            "signatures": set(), "authorizations": set(), "approvals": set(), "executions": set(),
        })
        request["signatures"].add(signature)
        if row.get("event") == "authorization":
            request["authorizations"].add(row.get("outcome") if isinstance(row.get("outcome"), str) else None)
        elif row.get("event") == "approval":
            request["approvals"].add(row.get("outcome") if isinstance(row.get("outcome"), str) else None)
        elif row.get("event") == "execution":
            request["executions"].add(row.get("outcome") if isinstance(row.get("outcome"), str) else None)

    count = 0
    for request in requests.values():
        # An attempt may be started before dispatch, but failed, denied or
        # malformed execution receipts conflict with a claimed success.
        if (len(request["signatures"]) != 1 or "succeeded" not in request["executions"]
                or request["executions"] - {"started", "succeeded"}):
            continue
        agent, actor, tag, tool = next(iter(request["signatures"]))
        if not (agent and actor and tool and isinstance(tag, str) and
                re.fullmatch(r"call:[0-9a-f]{64}", tag) and (tag[5:], agent) in completed):
            continue
        auth = request["authorizations"]
        approvals = request["approvals"]
        if auth == {"allow"} or (auth == {"require-approval"} and "approved" in approvals
                                 and not approvals - {"requested", "pending", "approved"}):
            count += 1
    return count



def _transcript_summary(voice_rows: list[dict[str, Any]], transcript_rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count disclosed two-sided transcript records paired to completed call lifecycle events.

    Structural evidence only: a local JSONL record cannot attest real SIP audio,
    client consent, provider delivery or independently signed lawful basis.
    Never return caller speech, room hashes, or summary text.
    """
    started: dict[tuple[str, str], int] = {}
    for row in voice_rows:
        room, agent, at = row.get("roomHash"), row.get("agent"), row.get("timestamp")
        if (row.get("type") == "CallStarted" and isinstance(room, str)
                and re.fullmatch(r"[0-9a-f]{64}", room) and isinstance(agent, str) and agent
                and type(at) is int):
            key = (room, agent)
            started[key] = min(at, started.get(key, at))
    completed: dict[tuple[str, str], int] = {}
    for row in voice_rows:
        room, agent, at = row.get("roomHash"), row.get("agent"), row.get("timestamp")
        key = (room, agent)
        if (row.get("type") == "CallEnded" and type(at) is int
                and key in started and at >= started[key]):
            completed[key] = max(at, completed.get(key, at))

    by_call: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in transcript_rows:
        if row.get("type") != "CallTranscript":
            continue
        room, agent = row.get("callHash"), row.get("agent")
        if isinstance(room, str) and isinstance(agent, str):
            by_call.setdefault((room, agent), []).append(row)

    complete = 0
    for key, records in by_call.items():
        # Conflicting duplicates veto proof, even if one copy looks valid.
        if len(records) != 1 or key not in completed:
            continue
        record = records[0]
        started_at, ended_at = record.get("timestamp"), record.get("endedAt")
        if (type(started_at) is not int or started_at != started[key]
                or type(ended_at) is not int or ended_at < started_at
                or ended_at > completed[key] + 60):
            continue
        disclosure = record.get("disclosure")
        # An asserted override owner is not proof of an independently signed
        # lawful-basis exception. Credit only an actually marked spoken notice.
        if (not isinstance(disclosure, dict) or disclosure.get("configured") is not True
                or disclosure.get("played") is not True):
            continue
        if (not isinstance(record.get("summary"), str) or not record["summary"].strip()
                or record.get("summarySource") not in ("extractive", "provided", "model", "fallback-extractive")
                or record.get("droppedTurns", 0) != 0):
            continue
        turns = record.get("turns")
        if not isinstance(turns, list) or not turns:
            continue
        speakers: set[str] = set()
        previous = -1.0
        valid = True
        for index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                valid = False
                break
            offset = turn.get("offsetSeconds")
            speaker, speech = turn.get("speaker"), turn.get("text")
            if (type(turn.get("index")) is not int or turn["index"] != index
                    or type(offset) not in (int, float) or not math.isfinite(offset)
                    or offset < previous or offset < 0
                    # Wall-clock timestamps are integer seconds; permit one second
                    # of rounding drift, never arbitrary speech beyond the call.
                    or offset > ended_at - started_at + 1
                    or speaker not in ("caller", "agent", "human")
                    or not isinstance(speech, str) or not speech.strip()):
                valid = False
                break
            previous = offset
            speakers.add(speaker)
        if valid and {"caller", "agent"} <= speakers:
            complete += 1
    return {"records": len(transcript_rows), "completeTwoWayCalls": complete}



@lru_cache(maxsize=1)
def _summary_contract():
    """Use the SAME canonical digest contract as the post-call worker."""
    path = pathlib.Path(__file__).resolve().parents[1] / "platform" / "channels" / "call_summary.py"
    spec = importlib.util.spec_from_file_location("reference_proof_summary_contract", path)
    if spec is None or spec.loader is None:
        raise ValueError("model summary contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_transcript_digest(record: dict[str, Any]) -> str:
    return _summary_contract().source_transcript_digest(record)


def _model_completion_summary(
    voice_rows: list[dict[str, Any]], transcript_rows: list[dict[str, Any]],
    completion_rows: list[dict[str, Any]],
) -> dict[str, int]:
    """Count model-labelled completions bound to the SAME original disclosed call.

    No summary, speech, identity, source digest or raw receipt leaves this
    function. A local model label/digest is not authenticated model execution,
    provider provenance or a client-approved transcript archive.
    """
    transcript_by_room: dict[str, list[dict[str, Any]]] = {}
    for row in transcript_rows:
        if row.get("type") == "CallTranscript" and isinstance(row.get("callHash"), str):
            transcript_by_room.setdefault(row["callHash"], []).append(row)
    completion_by_room: dict[str, list[dict[str, Any]]] = {}
    for row in completion_rows:
        if row.get("type") == "CallSummaryCompletion" and isinstance(row.get("callHash"), str):
            completion_by_room.setdefault(row["callHash"], []).append(row)
    room_agents: dict[str, set[str | None]] = {}
    for row in voice_rows:
        if row.get("type") not in ("CallStarted", "CallEnded"):
            continue
        room = row.get("roomHash")
        if isinstance(room, str):
            agent = row.get("agent")
            room_agents.setdefault(room, set()).add(agent if isinstance(agent, str) and agent else None)

    matched = 0
    for room, receipts in completion_by_room.items():
        calls = transcript_by_room.get(room, [])
        if (len(receipts) != 1 or len(calls) != 1
                or len(room_agents.get(room, ())) != 1 or None in room_agents[room]):
            continue
        receipt, call = receipts[0], calls[0]
        if (_transcript_summary(voice_rows, [call])["completeTwoWayCalls"] != 1
                or receipt.get("agent") != call.get("agent")
                or receipt.get("summarySource") != "model"
                or not isinstance(receipt.get("summary"), str)
                or not 0 < len(receipt["summary"]) <= 1200
                or type(receipt.get("timestamp")) is not int
                or receipt["timestamp"] < call["endedAt"]):
            continue
        if (not isinstance(receipt.get("transcriptDigest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt["transcriptDigest"]) is None
                or not isinstance(receipt.get("sourceTranscriptDigest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt["sourceTranscriptDigest"]) is None
                or receipt["sourceTranscriptDigest"] != _source_transcript_digest(call)
                or receipt.get("summaryDigest") != hashlib.sha256(receipt["summary"].encode("utf-8")).hexdigest()):
            continue
        matched += 1
    return {"records": len(completion_rows), "matchedModelCalls": matched}


def _single_call_scenario_count(
    voice_rows: list[dict[str, Any]], audit_rows: list[dict[str, Any]],
    transcript_rows: list[dict[str, Any]],
    completion_rows: list[dict[str, Any]] = (), *, require_model_summary: bool = False,
) -> int:
    """Structural intersection of disclosure, two-sided speech, action and fallback on ONE call.

    Counting each positive requirement independently can certify a reference
    scenario assembled from unrelated customers' calls. Reuse the individual
    strict correlation checks, but evaluate them against each unique call
    identity. The raw identifiers and speech never leave this function.
    A room reused by different agents is ambiguous and cannot prove acceptance.
    """
    room_agents: dict[str, set[str | None]] = {}
    for row in voice_rows:
        if row.get("type") not in ("CallStarted", "CallEnded"):
            continue
        room = row.get("roomHash")
        if not isinstance(room, str) or not re.fullmatch(r"[0-9a-f]{64}", room):
            continue
        agent = row.get("agent")
        room_agents.setdefault(room, set()).add(agent if isinstance(agent, str) and agent else None)

    complete = 0
    for room, agents in room_agents.items():
        if len(agents) != 1 or None in agents:
            continue
        agent = next(iter(agents))
        scoped = [
            row for row in voice_rows if row.get("roomHash") == room
            and (row.get("agent") == agent or (
                row.get("type") in ("HumanTransfer", "FallbackMessage")
                and row.get("agent") == agent
            ))
        ]
        if _voice_summary(scoped)["completedCallFallbackEvidence"] != 1:
            continue
        if _transcript_summary(scoped, transcript_rows)["completeTwoWayCalls"] != 1:
            continue
        if (require_model_summary and _model_completion_summary(
                scoped, transcript_rows, list(completion_rows))["matchedModelCalls"] != 1):
            continue
        # Check the entire audit log, not a prefiltered subset: another call
        # reusing the same request ID must still veto authorization proof.
        if _completed_call_actions(scoped, audit_rows) < 1:
            continue
        complete += 1
    return complete


def collect(project: str, *, read_transcripts: bool = False, read_model_summary_completions: bool = False) -> dict[str, Any]:
    if not PROJECT.fullmatch(project):
        raise ValueError("project name must use lowercase letters, numbers, and internal hyphens")
    services = _containers(project)
    service_summary = {
        name: {"status": info["status"], "running": "Up " in info["status"] or info["status"].startswith("Up")}
        for name, info in sorted(services.items())
    }
    audit_rows: list[dict[str, Any]] = []
    if "governed-tools" in services:
        audit_rows = _jsonl(services["governed-tools"]["container"], "/opt/data/.modelgarden/audit.jsonl")
    voice_rows: list[dict[str, Any]] = []
    if "voice" in services:
        voice_rows = _jsonl(services["voice"]["container"], "/opt/data/messages.jsonl")
    transcript_rows: list[dict[str, Any]] = []
    if (read_transcripts or read_model_summary_completions) and "voice" in services:
        # The fixed private default path is never supplied by a model or caller.
        transcript_rows = _jsonl(services["voice"]["container"], "/opt/data/transcripts.jsonl")
    completion_rows: list[dict[str, Any]] = []
    if read_model_summary_completions and "governed-worker" in services:
        # Fixed private worker journal; no caller-supplied path or output payload.
        completion_rows = _jsonl(
            services["governed-worker"]["container"],
            "/opt/data/.modelgarden/summary-completions/call-summary-completions.jsonl",
        )
    return {
        "schemaVersion": 1,
        "transcripts": _transcript_summary(voice_rows, transcript_rows),
        "modelSummaryCompletions": _model_completion_summary(
            voice_rows, transcript_rows, completion_rows,
        ) if read_model_summary_completions else {"records": 0, "matchedModelCalls": 0},
        "referenceScenario": {"completeCalls": _single_call_scenario_count(
            voice_rows, audit_rows, transcript_rows, completion_rows,
            require_model_summary=read_model_summary_completions,
        ) if (read_transcripts or read_model_summary_completions) else 0},
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project": project,
        "services": service_summary,
        "governance": {**_audit_summary(audit_rows), "completedCallActions": _completed_call_actions(voice_rows, audit_rows)},
        "voice": _voice_summary(voice_rows),
        "redaction": "No credentials, prompts, Tool arguments, caller identifiers, transfer targets, room identifiers, or message bodies are included.",
    }


def evaluate(evidence: dict[str, Any], *, require_voice_call: bool, require_fallback: bool, require_governed_action: bool, require_two_way_transcript: bool = False, require_model_summary_completion: bool = False) -> dict[str, Any]:
    failures: list[str] = []
    services = evidence["services"]
    needs_transcript = require_two_way_transcript or require_model_summary_completion
    if not services.get("hermes", {}).get("running"):
        failures.append("Hermes is not running")
    if "governed-tools" in services and not services["governed-tools"].get("running"):
        failures.append("governed-tools is not running")
    if "voice" in services and not services["voice"].get("running"):
        failures.append("voice is not running")
    if (require_voice_call or require_fallback) and not services.get("voice", {}).get("running"):
        failures.append("required voice runtime is absent or stopped")
    if require_governed_action and not services.get("governed-tools", {}).get("running"):
        failures.append("required governed Tool runtime is absent or stopped")
    if (require_voice_call or needs_transcript) and evidence["voice"].get("completedCalls", 0) < 1:
        failures.append("no completed voice call lifecycle pair exists")
    if (require_voice_call or needs_transcript) and evidence["voice"].get("transcriptWriteFailures", 0):
        failures.append("voice transcript write failures require investigation")
    if needs_transcript and not services.get("voice", {}).get("running"):
        failures.append("required voice runtime for two-way transcript is absent or stopped")
    if needs_transcript and evidence.get("transcripts", {}).get("completeTwoWayCalls", 0) < 1:
        failures.append("no complete disclosed two-way transcript is paired to a completed call")
    if require_model_summary_completion and not services.get("governed-worker", {}).get("running"):
        failures.append("required governed worker for model summary completion is absent or stopped")
    if require_model_summary_completion and evidence.get("modelSummaryCompletions", {}).get("matchedModelCalls", 0) < 1:
        failures.append("no model summary completion is bound to a disclosed two-way call")
    if require_fallback and evidence["voice"].get("completedCallFallbackEvidence", 0) < 1:
        failures.append("no human-transfer or message fallback correlated to a completed call")
    if require_governed_action and evidence["governance"].get("successfulActions", 0) < 1:
        failures.append("no successful governed business-action execution receipt exists")
    if require_governed_action and require_voice_call and evidence["governance"].get("completedCallActions", 0) < 1:
        failures.append("no authorized successful Tool request is explicitly correlated to a completed call")
    if (needs_transcript and require_fallback and require_governed_action
            and evidence.get("referenceScenario", {}).get("completeCalls", 0) < 1):
        failures.append("no same completed call has a disclosed two-way transcript, authorized Tool action and human fallback")
    return {"passed": not failures, "failures": failures}


def _markdown(evidence: dict[str, Any], result: dict[str, Any]) -> str:
    lines = [
        "# Model Garden reference proof",
        "",
        f"- Project: `{evidence['project']}`",
        f"- Generated: `{evidence['generatedAt']}`",
        f"- Collector checks: **{'PASS' if result['passed'] else 'INCOMPLETE'}** (not live-provider attestation)",
        "",
        "## Runtime services",
        "",
    ]
    for name, service in evidence["services"].items():
        lines.append(f"- {name}: {'running' if service['running'] else service['status']}")
    lines.extend([
        "",
        "## Evidence counts",
        "",
        f"- Governed action records: {evidence['governance']['records']}",
        f"- Successful governed executions: {evidence['governance']['successfulActions']}",
        f"- Authorized executions correlated to completed calls: {evidence['governance'].get('completedCallActions', 0)}",
        f"- Approval evidence records: {evidence['governance']['approvalEvidenceRecords']}",
        f"- Audit head hash present: {'yes' if evidence['governance'].get('auditHeadHash') else 'no'}",
        f"- Call starts observed: {evidence['voice']['distinctCalls']}",
        f"- Completed call lifecycle pairs: {evidence['voice']['completedCalls']}",
        f"- Completed calls with correlated fallback: {evidence['voice']['completedCallFallbackEvidence']}",
        f"- Transcript write failures: {evidence['voice']['transcriptWriteFailures']}",
        f"- Private transcript records inspected: {evidence.get('transcripts', {}).get('records', 0)}",
        f"- Complete disclosed two-way transcripts paired to a call: {evidence.get('transcripts', {}).get('completeTwoWayCalls', 0)}",
        f"- Model-labelled completions bound to a disclosed call: {evidence.get('modelSummaryCompletions', {}).get('matchedModelCalls', 0)}",
        f"- Same-call transcript, governed action and human fallback: {evidence.get('referenceScenario', {}).get('completeCalls', 0)}",
        f"- Human transfer evidence: {evidence['voice']['humanTransferEvidence']}",
        f"- Message fallback evidence: {evidence['voice']['messageFallbackEvidence']}",
    ])
    if result["failures"]:
        lines.extend(["", "## Remaining evidence", ""] + [f"- {item}" for item in result["failures"]])
    lines.extend(["", "> Passing collector checks does not prove real SIP, transcript content, provider receipts, or client acceptance.", f"> {evidence['redaction']}", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path(".model-garden-live-proof"))
    parser.add_argument("--require-voice-call", action="store_true")
    parser.add_argument("--require-two-way-transcript", action="store_true",
                        help="read the voice worker's private default transcript file and require a disclosed two-sided call")
    parser.add_argument("--require-model-summary-completion", action="store_true",
                        help="require a private worker model-labelled completion bound to an original disclosed call")
    parser.add_argument("--require-fallback", action="store_true")
    parser.add_argument("--require-governed-action", action="store_true")
    args = parser.parse_args()
    try:
        evidence = collect(args.project_name, read_transcripts=args.require_two_way_transcript,
                           read_model_summary_completions=args.require_model_summary_completion)
        result = evaluate(
            evidence,
            require_voice_call=args.require_voice_call,
            require_fallback=args.require_fallback,
            require_governed_action=args.require_governed_action,
            require_two_way_transcript=args.require_two_way_transcript,
            require_model_summary_completion=args.require_model_summary_completion,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {**evidence, "acceptance": result}
        (args.output_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (args.output_dir / "reference-proof.md").write_text(_markdown(evidence, result), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(args.output_dir / "reference-proof.md")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
