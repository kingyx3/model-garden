#!/usr/bin/env python3
"""Collect redacted, machine-readable evidence from a running reference deployment.

The collector never copies prompts, Tool arguments, caller numbers, message text, or
credentials into the evidence pack. It records service health and the existence/types
of governance and voice lifecycle records. Run it after deployment and again after the
real inbound-call/business-action acceptance scenario.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
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
    completed: dict[str, tuple[int, int]] = {}
    for row in rows:
        room, agent, timestamp = row.get("roomHash"), row.get("agent"), row.get("timestamp")
        if (row.get("type") == "CallEnded" and isinstance(room, str)
                and isinstance(agent, str) and type(timestamp) is int
                and (room, agent) in started and timestamp >= started[(room, agent)]):
            beginning = started[(room, agent)]
            if room not in completed:
                completed[room] = (beginning, timestamp)
    fallback_rooms = {
        row["roomHash"]
        for row in rows
        if row.get("type") in ("HumanTransfer", "FallbackMessage")
        and isinstance(row.get("roomHash"), str)
        and row["roomHash"] in completed
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


def collect(project: str) -> dict[str, Any]:
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
    return {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project": project,
        "services": service_summary,
        "governance": {**_audit_summary(audit_rows), "completedCallActions": _completed_call_actions(voice_rows, audit_rows)},
        "voice": _voice_summary(voice_rows),
        "redaction": "No credentials, prompts, Tool arguments, caller identifiers, transfer targets, room identifiers, or message bodies are included.",
    }


def evaluate(evidence: dict[str, Any], *, require_voice_call: bool, require_fallback: bool, require_governed_action: bool) -> dict[str, Any]:
    failures: list[str] = []
    services = evidence["services"]
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
    if require_voice_call and evidence["voice"].get("completedCalls", 0) < 1:
        failures.append("no completed voice call lifecycle pair exists")
    if require_voice_call and evidence["voice"].get("transcriptWriteFailures", 0):
        failures.append("voice transcript write failures require investigation")
    if require_fallback and evidence["voice"].get("completedCallFallbackEvidence", 0) < 1:
        failures.append("no human-transfer or message fallback correlated to a completed call")
    if require_governed_action and evidence["governance"].get("successfulActions", 0) < 1:
        failures.append("no successful governed business-action execution receipt exists")
    if require_governed_action and require_voice_call and evidence["governance"].get("completedCallActions", 0) < 1:
        failures.append("no authorized successful Tool request is explicitly correlated to a completed call")
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
    parser.add_argument("--require-fallback", action="store_true")
    parser.add_argument("--require-governed-action", action="store_true")
    args = parser.parse_args()
    try:
        evidence = collect(args.project_name)
        result = evaluate(
            evidence,
            require_voice_call=args.require_voice_call,
            require_fallback=args.require_fallback,
            require_governed_action=args.require_governed_action,
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
