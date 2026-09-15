#!/usr/bin/env python3
"""Authorize and execute Model Garden Tool actions through a minimal governed boundary.

MVP behavior is intentionally small and explicit:
- an Agent may request only Tools present in its compiled desired state;
- selected Tools resolve to allow or require-approval from the stricter of Agent and Tool configuration;
- approvals bind to an immutable requestId derived from agent + tool + exact arguments + initiating identity/context;
- denied, pending, approved, failed, and executed material actions can be written as JSONL audit events;
- first-party governance context can correlate tenant, conversation, runtime and model provenance;
- execution is injected by the caller, so this module never owns provider credentials.

This is not a general policy engine. Richer policy semantics belong behind the same
boundary only after repeated client requirements justify them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys
from collections.abc import Callable
from typing import Any

GOVERNANCE_CONTEXT_KEYS = (
    "tenantId",
    "conversationId",
    "sessionId",
    "runtime",
    "runtimeVersion",
    "modelProvider",
    "model",
    "modelConfigVersion",
    "credentialScope",
    "correlationId",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _agent_name(desired_state: dict[str, Any]) -> str:
    source = desired_state.get("source", {})
    name = source.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("Governed action failed: desired state is missing source.name")
    return name


def _selected_tool(desired_state: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    for tool in desired_state.get("tools", []):
        if tool.get("metadata", {}).get("name") == tool_name:
            return tool
    return None


def _governance_context(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Governed action failed: governance_context must be a JSON object")
    unknown = sorted(set(value) - set(GOVERNANCE_CONTEXT_KEYS))
    if unknown:
        raise ValueError("Governed action failed: unsupported governance context keys: " + ", ".join(unknown))
    context: dict[str, Any] = {}
    for key in GOVERNANCE_CONTEXT_KEYS:
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or not item:
                raise ValueError(f"Governed action failed: governance context {key} must be a non-empty string")
            context[key] = item
    return context


def build_action_request(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    initiating_user: str | None = None,
    governance_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("Governed action failed: arguments must be a JSON object")

    material: dict[str, Any] = {
        "agent": _agent_name(desired_state),
        "tool": tool_name,
        "arguments": arguments,
    }
    if initiating_user:
        material["initiatingUser"] = initiating_user
    context = _governance_context(governance_context)
    if context:
        material["governance"] = context
    request_id = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return {**material, "requestId": request_id}


def authorize_action(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    initiating_user: str | None = None,
    governance_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = build_action_request(
        desired_state,
        tool_name,
        arguments,
        initiating_user=initiating_user,
        governance_context=governance_context,
    )
    tool = _selected_tool(desired_state, tool_name)
    if tool is None:
        return {"decision": "deny", "reason": "tool-not-selected", "request": request}

    agent_requires_approval = desired_state.get("agent", {}).get("spec", {}).get("approvals", {}).get("externalActions") == "required"
    tool_requires_approval = tool.get("spec", {}).get("approvalRequired", False)
    if agent_requires_approval or tool_requires_approval:
        reason = "agent-requires-approval" if agent_requires_approval else "tool-requires-approval"
        return {"decision": "require-approval", "reason": reason, "request": request}
    return {"decision": "allow", "reason": "selected-tool", "request": request}


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def make_audit_event(event_type: str, request: dict[str, Any], *, outcome: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {
        "timestamp": _utc_timestamp(),
        "event": event_type,
        "outcome": outcome,
        "requestId": request["requestId"],
        "agent": request["agent"],
        "tool": request["tool"],
        "arguments": request["arguments"],
    }
    if "initiatingUser" in request:
        event["initiatingUser"] = request["initiatingUser"]
    if "governance" in request:
        event["governance"] = request["governance"]
    if details:
        event["details"] = details
    return event


def append_audit_event(path: pathlib.Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(event) + "\n")


def _result_receipt(result: Any) -> dict[str, Any]:
    encoded = _canonical_json(result)
    return {
        "resultType": type(result).__name__,
        "resultDigest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def execute_action(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    executor: Callable[[dict[str, Any]], Any],
    *,
    approval: dict[str, Any] | None = None,
    initiating_user: str | None = None,
    governance_context: dict[str, Any] | None = None,
    audit_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    authorization = authorize_action(desired_state, tool_name, arguments, initiating_user=initiating_user, governance_context=governance_context)
    request = authorization["request"]
    append_audit_event(audit_path, make_audit_event("authorization", request, outcome=authorization["decision"], details={"reason": authorization["reason"]}))

    if authorization["decision"] == "deny":
        raise PermissionError(f"Tool {tool_name!r} is not selected by Agent {request['agent']!r}")

    if authorization["decision"] == "require-approval":
        if approval is None:
            append_audit_event(audit_path, make_audit_event("approval", request, outcome="pending"))
            return {"status": "pending-approval", "request": request}
        if approval.get("requestId") != request["requestId"]:
            append_audit_event(audit_path, make_audit_event("approval", request, outcome="rejected", details={"reason": "request-id-mismatch"}))
            raise PermissionError("Approval does not match the exact material action request")
        if approval.get("approved") is not True:
            append_audit_event(audit_path, make_audit_event("approval", request, outcome="rejected", details={"reason": "not-approved"}))
            raise PermissionError("Action approval was not granted")
        approver = approval.get("approver")
        if not isinstance(approver, str) or not approver:
            raise ValueError("Approved action must record a non-empty approver")
        append_audit_event(audit_path, make_audit_event("approval", request, outcome="approved", details={"approver": approver}))

    try:
        result = executor(arguments)
    except Exception as exc:
        append_audit_event(audit_path, make_audit_event("execution", request, outcome="failed", details={"errorType": type(exc).__name__}))
        raise
    append_audit_event(audit_path, make_audit_event("execution", request, outcome="succeeded", details=_result_receipt(result)))
    return {"status": "executed", "request": request, "result": result}


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("desired_state", help="Compiled single-Agent desired-state JSON")
    parser.add_argument("tool", help="Tool metadata.name")
    parser.add_argument("--arguments", default="{}", help="Action arguments as a JSON object")
    parser.add_argument("--initiating-user", help="Optional initiating user/workload identity")
    parser.add_argument("--governance-context", default="{}", help="First-party governance context as a JSON object")
    args = parser.parse_args()

    try:
        desired_state = _load_json(pathlib.Path(args.desired_state))
        arguments = json.loads(args.arguments)
        governance_context = json.loads(args.governance_context)
        if not isinstance(arguments, dict):
            raise ValueError("--arguments must decode to a JSON object")
        if not isinstance(governance_context, dict):
            raise ValueError("--governance-context must decode to a JSON object")
        result = authorize_action(desired_state, args.tool, arguments, initiating_user=args.initiating_user, governance_context=governance_context)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["decision"] != "deny" else 2


if __name__ == "__main__":
    raise SystemExit(main())
