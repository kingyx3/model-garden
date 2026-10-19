"""Query, task timeline, policy replay and sandbox dry-run over the Action Ledger, plus CSV and JSON export (#75, #72).

The ledger (``audit.jsonl``, schema v3, hash-chained and verified by ``govern-action.verify_audit_chain``) records every governed
action with redacted arguments and digests. This module answers the operator's questions from it without touching a live system:

- **query**: filter events by Agent, principal, approver, capability, provider, tool, outcome, time range, task, correlation id and
  external object. ``principal`` matches the initiating user and the originating principal; ``approver`` matches the person who
  approved or who a hand-off was completed by. Events that carry no Agent (policy changes, operator actions) are matched only by
  time, principal-free filters, and are left out when an Agent or task filter is given, so a filter never pulls in unrelated records.
- **timeline**: one task (``taskId``) or correlation (``correlationId``) as ordered steps grouped by request, with reads, delegation
  chain, approvals, executions, hand-offs and the usage (tokens, cost, steps) any event reports, joined across Agents.
- **policy replay**: re-evaluates each recorded authorization against a policy (the current compiled desired state, or a historical
  one supplied) and reports differences. Arguments in the ledger are redacted, so a rule whose conditions depend on argument values or
  a daily limit cannot be reproduced exactly; such results are marked ``inputsIncomplete`` instead of being claimed as a match.
- **dry-run replay**: walks the recorded executions against *sandbox* connectors only. Only ``SandboxConnector`` objects are accepted,
  and anything else, including any callable that could reach a live provider, is refused when the replay is built. It writes nothing
  to the ledger, calls no network and skips steps whose arguments were redacted unless the sandbox says it can take redacted input.
- **export**: JSON or CSV (spreadsheet formulas neutralised) of any of the above for DDQs and auditors.

It reads a ledger and a desired state and holds no credential. The CLI is ``client-operator.py audit``.
"""
from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import io
import json
import pathlib
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

REDACTED = "[redacted]"
MAX_EVENTS = 200_000
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


class AuditQueryError(ValueError):
    """The ledger or an input to a query is unusable; the message names what, never record contents."""


class LiveConnectorRefused(AuditQueryError):
    """A dry-run replay was given something that is not a sandbox connector."""


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parents[2] / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _policy():
    return _load("model_garden_audit_query_policy", "platform/core/policy.py")


def read_ledger(path: pathlib.Path) -> list[dict[str, Any]]:
    """Every event in the ledger, in chain order, each annotated with its 1-based ``_line``."""
    events: list[dict[str, Any]] = []
    try:
        with pathlib.Path(path).open("r", encoding="utf-8") as handle:
            for number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except ValueError as exc:
                    raise AuditQueryError(f"ledger line {number} is not valid JSON") from exc
                if not isinstance(event, dict):
                    raise AuditQueryError(f"ledger line {number} is not an object")
                events.append({**event, "_line": number})
                if len(events) > MAX_EVENTS:
                    raise AuditQueryError(f"ledger has more than {MAX_EVENTS} events; query a time slice of an export instead")
    except OSError as exc:
        raise AuditQueryError(f"cannot read the ledger ({type(exc).__name__})") from None
    return events


def verify(path: pathlib.Path) -> dict[str, Any]:
    """The chain verdict from the governance module: ``{"valid", "events", "headHash"}`` or ``{"valid": False, "error"}``."""
    govern = _load("model_garden_audit_query_govern", "scripts/govern-action.py")
    try:
        return govern.verify_audit_chain(pathlib.Path(path))
    except ValueError as exc:
        return {"valid": False, "error": str(exc)}


# -- query ------------------------------------------------------------------------------------------------

def _parse_time(value: str | None, label: str) -> dt.datetime | None:
    if value is None:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AuditQueryError(f"{label} must be a date (2026-10-01) or an ISO time (2026-10-01T09:00:00Z)") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _when(event: Mapping[str, Any]) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _principals(event: Mapping[str, Any]) -> set[str]:
    governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
    found = {event.get("initiatingUser"), governance.get("originatingPrincipal"), event.get("changedBy"), event.get("performedBy")}
    return {item for item in found if isinstance(item, str)}


def _approvers(event: Mapping[str, Any]) -> set[str]:
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    found = {details.get("approver"), details.get("completedBy"), event.get("approver")}
    return {item for item in found if isinstance(item, str)}


@dataclass(frozen=True)
class Filter:
    agent: str | None = None
    principal: str | None = None
    approver: str | None = None
    capability: str | None = None
    provider: str | None = None
    tool: str | None = None
    event: str | None = None
    outcome: str | None = None
    since: str | None = None
    until: str | None = None
    task: str | None = None
    correlation: str | None = None
    external_object: str | None = None
    request: str | None = None

    def matches(self, event: Mapping[str, Any], _since=None, _until=None) -> bool:
        ledger = event.get("ledger") if isinstance(event.get("ledger"), Mapping) else {}
        governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
        checks: list[bool] = []
        if self.agent is not None:
            checks.append(event.get("agent") == self.agent)
        if self.principal is not None:
            checks.append(self.principal in _principals(event))
        if self.approver is not None:
            checks.append(self.approver in _approvers(event))
        if self.capability is not None:
            checks.append(ledger.get("capability") == self.capability)
        if self.provider is not None:
            checks.append(ledger.get("provider") == self.provider)
        if self.tool is not None:
            checks.append(event.get("tool") == self.tool)
        if self.event is not None:
            checks.append(event.get("event") == self.event)
        if self.outcome is not None:
            checks.append(event.get("outcome") == self.outcome)
        if self.task is not None:
            checks.append(governance.get("taskId") == self.task or _names_task(event, self.task))
        if self.correlation is not None:
            checks.append(governance.get("correlationId") == self.correlation)
        if self.external_object is not None:
            checks.append(ledger.get("externalObjectRef") == self.external_object)
        if self.request is not None:
            checks.append(isinstance(event.get("requestId"), str) and event["requestId"].startswith(self.request))
        if _since is not None or _until is not None:
            moment = _when(event)
            checks.append(moment is not None and (_since is None or moment >= _since) and (_until is None or moment < _until))
        return all(checks)


def query(events: Iterable[Mapping[str, Any]], criteria: Filter) -> list[dict[str, Any]]:
    since, until = _parse_time(criteria.since, "since"), _parse_time(criteria.until, "until")
    return [dict(event) for event in events if criteria.matches(event, since, until)]


# -- timeline ---------------------------------------------------------------------------------------------

_TASK_KEYS = ("taskId", "fromTask", "toTask")
_STEP_DETAILS = ("fromTask", "toTask", "fromAgent", "toAgent", "taskId", "step", "outcome", "assignee", "late", "exceeded", "reason", "depth", "chain",
                 "id", "kind", "digest", "reportLength", "policy", "branches", "concurrency", "succeeded", "failed", "cancelled", "refused", "budgetExhausted")


def _names_task(event: Mapping[str, Any], task: str) -> bool:
    """An operator event (a delegation hop, a refused hop, a person's step, an exhausted budget) that names the task in its details."""
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    return event.get("event") == "operator-action" and any(details.get(key) == task for key in _TASK_KEYS)


def _usage(event: Mapping[str, Any]) -> dict[str, int]:
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    usage = details.get("usage") if isinstance(details.get("usage"), Mapping) else {}
    return {key: int(usage[key]) for key in ("tokens", "costCents", "steps") if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)}


def timeline(events: Iterable[Mapping[str, Any]], *, task: str | None = None, correlation: str | None = None) -> dict[str, Any]:
    """Reads, delegations, approvals, writes and usage across Agents for one task or correlation, in ledger order."""
    if (task is None) == (correlation is None):
        raise AuditQueryError("a timeline needs exactly one of a task id or a correlation id")
    selected = query(events, Filter(task=task, correlation=correlation))
    requests: dict[str, dict[str, Any]] = {}
    totals = {"tokens": 0, "costCents": 0, "steps": 0}
    steps: list[dict[str, Any]] = []
    for event in sorted(selected, key=lambda item: (item.get("sequence") or item["_line"])):
        governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
        kind = event.get("event")
        used = _usage(event)
        for key, amount in used.items():
            totals[key] += amount
        step = {
            "sequence": event.get("sequence"), "timestamp": event.get("timestamp"), "event": kind, "outcome": event.get("outcome"),
            "agent": event.get("agent"), "tool": event.get("tool"), "requestId": event.get("requestId"),
            "delegationChain": list(governance.get("delegationChain") or []),
            "parentRequestId": governance.get("parentRequestId"),
            **({"approver": sorted(_approvers(event))[0]} if _approvers(event) else {}),
            **({"reads": [{"sourceId": item.get("sourceId"), "recordCount": item.get("recordCount")} for item in event["details"].get("reads", [])]}
               if kind == "read" and isinstance(event.get("details"), Mapping) else {}),
            **({"usage": used} if used else {}),
        }
        if kind == "operator-action":  # a hop, a refused hop, a person's answer or a halted budget: who and what, never goal or note text
            details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
            step["action"], step["by"] = event.get("action"), event.get("performedBy")
            step["details"] = {key: details[key] for key in _STEP_DETAILS if key in details}
        steps.append(step)
        request_id = event.get("requestId")
        if isinstance(request_id, str):
            entry = requests.setdefault(request_id, {"requestId": request_id, "agent": event.get("agent"), "tool": event.get("tool"), "events": [], "final": None})
            entry["events"].append(kind)
            if kind in ("execution", "handoff") or (kind == "authorization" and event.get("outcome") == "deny"):
                entry["final"] = f"{kind}:{event.get('outcome')}"
    agents = sorted({step["agent"] for step in steps if step["agent"]}
                    | {name for step in steps for name in (step.get("details") or {}).get("chain", [])})
    return {
        "task": task, "correlation": correlation, "agents": agents, "requests": list(requests.values()), "steps": steps,
        "usage": totals, "events": len(steps),
    }


def call_correlation(call_hash: str) -> str:
    """The correlation id a call's governed Tool calls carry when the runtime tags them with the call (``call:<callHash>``)."""
    return f"call:{call_hash}"


def request_timeline(events: Iterable[Mapping[str, Any]], request_ids: Iterable[str] = (), *, call: str | None = None) -> dict[str, Any]:
    """Every ledger event of one or more requests together: authorization, approval, execution, hand-off and the child requests.

    A request is named by its full ``requestId`` or a prefix of at least 8 characters. ``call`` adds every event whose correlation
    id is ``call:<callHash>``, so one phone call reads as one story whether its request ids were recorded on the transcript or not.
    A requested id with no event is listed under ``missing`` rather than dropped, so a gap is visible.
    """
    wanted = [item for item in dict.fromkeys(request_ids) if isinstance(item, str) and item]
    if not wanted and not call:
        raise AuditQueryError("a request timeline needs at least one request id or a call hash")
    for item in wanted:
        if len(item) < 8:
            raise AuditQueryError("a request id (or prefix) is at least 8 characters")
    correlation = call_correlation(call) if call else None
    found: set[str] = set()
    selected: list[Mapping[str, Any]] = []
    for event in events:
        governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
        request_id, parent = event.get("requestId"), governance.get("parentRequestId")
        hits = [w for w in wanted if (isinstance(request_id, str) and request_id.startswith(w)) or (isinstance(parent, str) and parent.startswith(w))]
        if hits or (correlation is not None and governance.get("correlationId") == correlation):
            selected.append(event)
            found.update(hits)
    selected.sort(key=lambda item: (item.get("sequence") or item.get("_line") or 0))
    requests: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    for event in selected:
        governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
        kind = event.get("event")
        step = {
            "sequence": event.get("sequence"), "timestamp": event.get("timestamp"), "event": kind, "outcome": event.get("outcome"),
            "agent": event.get("agent"), "tool": event.get("tool"), "requestId": event.get("requestId"),
            "parentRequestId": governance.get("parentRequestId"),
            **({"approver": sorted(_approvers(event))[0]} if _approvers(event) else {}),
        }
        steps.append(step)
        request_id = event.get("requestId")
        if isinstance(request_id, str):
            entry = requests.setdefault(request_id, {"requestId": request_id, "tool": event.get("tool"), "events": [], "final": None})
            entry["events"].append(kind)
            if kind in ("execution", "handoff") or (kind == "authorization" and event.get("outcome") == "deny"):
                entry["final"] = f"{kind}:{event.get('outcome')}"
    return {"call": call, "requested": wanted, "missing": [w for w in wanted if w not in found], "requests": list(requests.values()),
            "steps": steps, "events": len(steps)}


# -- policy replay ----------------------------------------------------------------------------------------

def _tool_spec(desired_state: Mapping[str, Any], name: Any) -> Mapping[str, Any] | None:
    for tool in desired_state.get("tools", []):
        if isinstance(tool, Mapping) and tool.get("metadata", {}).get("name") == name:
            return tool
    return None


def _principal_access(event: Mapping[str, Any]) -> str:
    ledger = event.get("ledger") if isinstance(event.get("ledger"), Mapping) else {}
    if ledger.get("principalAccess") in ("member", "public"):
        return ledger["principalAccess"]
    return "public" if str(event.get("initiatingUser", "")).startswith("unmapped:") else "member"


def replay_policy(events: Iterable[Mapping[str, Any]], desired_state: Mapping[str, Any]) -> dict[str, Any]:
    """Re-evaluate every recorded authorization against ``desired_state``'s policy and report where the decision differs."""
    policy = _policy()
    policies = desired_state.get("policies")
    rules = policy.rules_of(policies)
    conditional = any(isinstance(rule.get("conditions"), Mapping) and rule["conditions"] for rule in rules)
    agent_spec = desired_state.get("agent", {}).get("spec", {})
    report: dict[str, Any] = {"checked": 0, "same": 0, "different": 0, "incomplete": 0, "differences": [],
                              "replayedPolicyVersion": policy.policy_version(policies)}
    for event in events:
        if event.get("event") != "authorization":
            continue
        report["checked"] += 1
        ledger = event.get("ledger") if isinstance(event.get("ledger"), Mapping) else {}
        governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
        recorded = {"decision": event.get("outcome"), "reason": (event.get("details") or {}).get("reason"), "ruleId": ledger.get("policyRuleId")}
        tool = _tool_spec(desired_state, event.get("tool"))
        if tool is None:
            replayed = {"decision": "deny", "reason": "tool-not-selected", "ruleId": None}
        else:
            outcome = policy.evaluate(
                policies, tool_name=event["tool"], risk=tool.get("spec", {}).get("risk"),
                classification=agent_spec.get("security", {}).get("dataClassification"), channel=governance.get("channel"),
                principal=_principal_access(event), arguments=event.get("arguments") if isinstance(event.get("arguments"), Mapping) else None,
                agent_requires_approval=agent_spec.get("approvals", {}).get("externalActions") == "required",
                tool_requires_approval=bool(tool.get("spec", {}).get("approvalRequired", False)),
            )
            replayed = {"decision": outcome["decision"], "reason": outcome["reason"], "ruleId": outcome.get("ruleId")}
        incomplete = conditional and tool is not None
        if incomplete:
            report["incomplete"] += 1
        if recorded["decision"] == replayed["decision"] and recorded["reason"] == replayed["reason"]:
            report["same"] += 1
            continue
        report["different"] += 1
        report["differences"].append({
            "requestId": event.get("requestId"), "sequence": event.get("sequence"), "timestamp": event.get("timestamp"),
            "agent": event.get("agent"), "tool": event.get("tool"), "recorded": recorded, "replayed": replayed,
            "recordedPolicyVersion": ledger.get("policyVersion"), "inputsIncomplete": incomplete,
        })
    return report


# -- dry-run replay ---------------------------------------------------------------------------------------

class SandboxConnector:
    """Marker base for connectors a dry-run may call. A subclass must never reach a live provider."""

    accepts_redacted = False
    sandbox = True

    def run(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        raise NotImplementedError


class MockConnector(SandboxConnector):
    """Records the call and returns a fixed, clearly-fake result; it has no network access."""

    accepts_redacted = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool, dict(arguments)))
        return {"sandbox": True, "tool": tool, "externalObjectRef": f"sandbox:{tool}"}


def dry_run_replay(
    events: Iterable[Mapping[str, Any]], sandboxes: Mapping[str, SandboxConnector], *, default: SandboxConnector | None = None,
) -> dict[str, Any]:
    """Replay recorded executions against sandbox connectors only; live connectors are refused when the replay is built."""
    for name, connector in {**sandboxes, **({"*": default} if default is not None else {})}.items():
        if not isinstance(connector, SandboxConnector) or getattr(connector, "sandbox", False) is not True:
            raise LiveConnectorRefused(f"{name}: a dry-run accepts SandboxConnector objects only; live connectors are refused")
    steps: list[dict[str, Any]] = []
    executed = skipped = 0
    for event in events:
        if event.get("event") != "execution":
            continue
        tool = str(event.get("tool"))
        connector = sandboxes.get(tool) or default
        arguments = event.get("arguments") if isinstance(event.get("arguments"), Mapping) else {}
        step: dict[str, Any] = {"requestId": event.get("requestId"), "sequence": event.get("sequence"), "tool": tool, "recorded": event.get("outcome")}
        if connector is None:
            step.update(status="skipped", reason="no-sandbox-for-tool")
        elif REDACTED in arguments.values() and not connector.accepts_redacted:
            step.update(status="skipped", reason="arguments-redacted")
        else:
            try:
                result = connector.run(tool, arguments)
            except Exception as exc:  # noqa: BLE001 - report the type only
                step.update(status="failed", error=type(exc).__name__)
            else:
                step.update(status="replayed", sandboxResult=result)
        if step["status"] == "skipped":
            skipped += 1
        else:
            executed += 1
        steps.append(step)
    return {"mode": "sandbox", "replayed": executed, "skipped": skipped, "steps": steps}


# -- export -----------------------------------------------------------------------------------------------

EVENT_COLUMNS = (
    "sequence", "timestamp", "event", "outcome", "requestId", "agent", "tool", "initiatingUser", "originatingPrincipal", "channel",
    "taskId", "correlationId", "capability", "provider", "tier", "policyRuleId", "policyVersion", "externalObjectRef", "approver", "argumentsDigest",
)


def flatten(event: Mapping[str, Any]) -> dict[str, Any]:
    ledger = event.get("ledger") if isinstance(event.get("ledger"), Mapping) else {}
    governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
    approvers = sorted(_approvers(event))
    return {
        "sequence": event.get("sequence"), "timestamp": event.get("timestamp"), "event": event.get("event"), "outcome": event.get("outcome"),
        "requestId": event.get("requestId"), "agent": event.get("agent"), "tool": event.get("tool"),
        "initiatingUser": event.get("initiatingUser") or event.get("changedBy") or event.get("performedBy"),
        "originatingPrincipal": governance.get("originatingPrincipal"), "channel": governance.get("channel"),
        "taskId": governance.get("taskId"), "correlationId": governance.get("correlationId"),
        "capability": ledger.get("capability"), "provider": ledger.get("provider"), "tier": ledger.get("tier"),
        "policyRuleId": ledger.get("policyRuleId"), "policyVersion": ledger.get("policyVersion"),
        "externalObjectRef": ledger.get("externalObjectRef"), "approver": approvers[0] if approvers else None,
        "argumentsDigest": event.get("argumentsDigest"),
    }


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def to_csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str] = EVENT_COLUMNS) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in columns])
    return buffer.getvalue()


def to_json(document: Any) -> str:
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key != "_line"}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return json.dumps(clean(document), indent=2, sort_keys=True) + "\n"


def event_rows(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [flatten(event) for event in events]
