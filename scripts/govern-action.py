#!/usr/bin/env python3
"""Authorize and execute Model Garden Tool actions through a minimal governed boundary.

MVP behavior is intentionally small and explicit:
- an Agent may request only Tools present in its compiled desired state;
- selected Tools resolve to allow or require-approval from the Tool contract;
- approvals bind to an immutable requestId derived from agent + tool + arguments;
- denied, pending, approved, and executed material actions can be written as JSONL audit events;
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


def build_action_request(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    initiating_user: str | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("Governed action failed: arguments must be a JSON object")

    material = {
        "agent": _agent_name(desired_state),
        "tool": tool_name,
        "arguments": arguments,
    }
    request_id = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    request = {**material, "requestId": request_id}
    if initiating_user:
        request["initiatingUser"] = initiating_user
    return request


def authorize_action(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    initiating_user: str | None = None,
) -> dict[str, Any]:
    request = build_action_request(
        desired_state,
        tool_name,
        arguments,
        initiating_user=initiating_user,
    )
    tool = _selected_tool(desired_state, tool_name)
    if tool is None:
        return {
            "decision": "deny",
            "reason": "tool-not-selected",
            "request": request,
        }

    spec = tool.get("spec", {})
    if spec.get("approvalRequired", False):
        return {
            "decision": "require-approval",
            "reason": "tool-requires-approval",
            "request": request,
        }

    return {
        "decision": "allow",
        "reason": "selected-tool",
        "request": request,
    }


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def make_audit_event(
    event_type: str,
    request: dict[str, Any],
    *,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    if details:
        event["details"] = details
    return event


def append_audit_event(path: pathlib.Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(event) + "\n")


def execute_action(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    executor: Callable[[dict[str, Any]], Any],
    *,
    approval: dict[str, Any] | None = None,
    initiating_user: str | None = None,
    audit_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    authorization = authorize_action(
        desired_state,
        tool_name,
        arguments,
        initiating_user=initiating_user,
    )
    request = authorization["request"]
    append_audit_event(
        audit_path,
        make_audit_event(
            "authorization",
            request,
            outcome=authorization["decision"],
            details={"reason": authorization["reason"]},
        ),
    )

    if authorization["decision"] == "deny":
        raise PermissionError(f"Tool {tool_name!r} is not selected by Agent {request['agent']!r}")

    if authorization["decision"] == "require-approval":
        if approval is None:
            append_audit_event(
                audit_path,
                make_audit_event("approval", request, outcome="pending"),
            )
            return {
                "status": "pending-approval",
                "request": request,
            }
        if approval.get("requestId") != request["requestId"]:
            append_audit_event(
                audit_path,
                make_audit_event("approval", request, outcome="rejected", details={"reason": "request-id-mismatch"}),
            )
            raise PermissionError("Approval does not match the exact material action request")
        if approval.get("approved") is not True:
            append_audit_event(
                audit_path,
                make_audit_event("approval", request, outcome="rejected", details={"reason": "not-approved"}),
            )
            raise PermissionError("Action approval was not granted")
        approver = approval.get("approver")
        if not isinstance(approver, str) or not approver:
            raise ValueError("Approved action must record a non-empty approver")
        append_audit_event(
            audit_path,
            make_audit_event("approval", request, outcome="approved", details={"approver": approver}),
        )

    result = executor(arguments)
    append_audit_event(
        audit_path,
        make_audit_event("execution", request, outcome="succeeded"),
    )
    return {
        "status": "executed",
        "request": request,
        "result": result,
    }


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
    args = parser.parse_args()

    try:
        desired_state = _load_json(pathlib.Path(args.desired_state))
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("--arguments must decode to a JSON object")
        result = authorize_action(
            desired_state,
            args.tool,
            arguments,
            initiating_user=args.initiating_user,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["decision"] != "deny" else 2


if __name__ == "__main__":
    raise SystemExit(main())
