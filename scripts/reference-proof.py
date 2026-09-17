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
        if len(parts) == 3 and parts[0]:
            services[parts[0]] = {"status": parts[1], "container": parts[2]}
    return services


def _jsonl(container: str, path: str) -> list[dict[str, Any]]:
    result = _run(["docker", "exec", container, "sh", "-c", f"test -f {path} && cat {path} || true"])
    if result.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
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
        "auditHeadHash": head_hash,
    }


def _voice_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    types = Counter(row.get("type") for row in rows if isinstance(row.get("type"), str))
    call_ids = {
        row.get("roomHash") or row.get("room")
        for row in rows
        if row.get("type") == "CallStarted" and isinstance(row.get("roomHash") or row.get("room"), str)
    }
    return {
        "records": len(rows),
        "eventTypes": dict(sorted(types.items())),
        "distinctCalls": len(call_ids),
        "humanTransferEvidence": types.get("HumanTransfer", 0),
        "messageFallbackEvidence": types.get("FallbackMessage", 0),
    }


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
        "governance": _audit_summary(audit_rows),
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
    if require_voice_call and evidence["voice"]["distinctCalls"] < 1:
        failures.append("no real voice call lifecycle evidence exists")
    if require_fallback and evidence["voice"]["humanTransferEvidence"] + evidence["voice"]["messageFallbackEvidence"] < 1:
        failures.append("no human-transfer or message-capture fallback evidence exists")
    if require_governed_action and evidence["governance"]["records"] < 1:
        failures.append("no governed business-action audit evidence exists")
    return {"passed": not failures, "failures": failures}


def _markdown(evidence: dict[str, Any], result: dict[str, Any]) -> str:
    lines = [
        "# Model Garden reference proof",
        "",
        f"- Project: `{evidence['project']}`",
        f"- Generated: `{evidence['generatedAt']}`",
        f"- Result: **{'PASS' if result['passed'] else 'INCOMPLETE'}**",
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
        f"- Approval evidence records: {evidence['governance']['approvalEvidenceRecords']}",
        f"- Audit head hash present: {'yes' if evidence['governance'].get('auditHeadHash') else 'no'}",
        f"- Distinct live calls: {evidence['voice']['distinctCalls']}",
        f"- Human transfer evidence: {evidence['voice']['humanTransferEvidence']}",
        f"- Message fallback evidence: {evidence['voice']['messageFallbackEvidence']}",
    ])
    if result["failures"]:
        lines.extend(["", "## Remaining evidence", ""] + [f"- {item}" for item in result["failures"]])
    lines.extend(["", f"> {evidence['redaction']}", ""])
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
