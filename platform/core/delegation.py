"""Task envelopes and authority attenuation for delegation between Agents.

A coordinator may hand work to a specialist only within what the person who asked could have asked for.
Adding Agents must never create a path to do more than a human could, so authority only ever shrinks as a
task is delegated:

    effective authority of a delegate = its own grants  ∩  the delegator's authority for this task
                                        ∩  the originating principal's permissions

The envelope is the unit that travels with a task: goal, constraints, deadline, budget, the Tools the task
may use, the originating principal, the parent task and the chain of Agents so far. This module is pure
(no I/O beyond an optional ledger helper) and credential-free. It is not wired into a runtime: the governed
sidecar still serves one Agent per environment (#95), so nothing here yet decides a live call. It supplies
the rules, the governance context fields (`originatingPrincipal`, `taskId`, `delegationChain`) and the ledger
event a multi-Agent runtime has to use, and tests that no delegation path escalates.

Rules enforced on every hop (each refusal raises ``DelegationError`` and delegates nothing):

- the delegate's required Tools must be inside its effective authority (it is refused, not silently narrowed);
- depth and fan-out are bounded, and an Agent already in the chain is a loop;
- the child's budget and deadline can only be tighter than what the parent has left, and a parent whose budget
  is exhausted cannot delegate;
- the originating principal and the task's authority are fixed by the root: a child cannot change either.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

BUDGET_KEYS = ("tokens", "costCents", "steps")
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_FAN_OUT = 5
MAX_DEPTH_CEILING = 8
MAX_FAN_OUT_CEILING = 20
MAX_GOAL = 500
MAX_CONSTRAINTS = 10
MAX_CONSTRAINT = 200
MAX_TOOLS = 200
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_TOOL = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$")
_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DelegationError(PermissionError):
    """A delegation that would escalate authority, loop, overrun a bound or is malformed."""


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise DelegationError(f"{label} must be a short identifier")
    return value


def _task_id(value: Any, label: str = "task id") -> str:
    if not isinstance(value, str) or not _TASK.match(value):
        raise DelegationError(f"{label} must be a short identifier")
    return value


def _tools(values: Iterable[Any] | None, label: str) -> frozenset[str]:
    items = list(values or [])
    if len(items) > MAX_TOOLS or not all(isinstance(item, str) and _TOOL.match(item) for item in items):
        raise DelegationError(f"{label} must be portable Tool names such as calendar.book")
    return frozenset(items)


def _budget(value: Mapping[str, Any] | None, label: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DelegationError(f"{label} must be an object")
    unknown = sorted(set(value) - set(BUDGET_KEYS))
    if unknown:
        raise DelegationError(f"{label} has unsupported keys: {', '.join(unknown)}")
    result: dict[str, int] = {}
    for key, amount in value.items():
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise DelegationError(f"{label}.{key} must be a non-negative integer")
        result[key] = amount
    return result


def _limits(max_depth: int, max_fan_out: int) -> None:
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 1 <= max_depth <= MAX_DEPTH_CEILING:
        raise DelegationError(f"maximum delegation depth must be 1 to {MAX_DEPTH_CEILING}")
    if not isinstance(max_fan_out, int) or isinstance(max_fan_out, bool) or not 1 <= max_fan_out <= MAX_FAN_OUT_CEILING:
        raise DelegationError(f"maximum fan-out must be 1 to {MAX_FAN_OUT_CEILING}")


def _constraints(values: Iterable[Any] | None) -> list[str]:
    items = list(values or [])
    if len(items) > MAX_CONSTRAINTS or not all(isinstance(item, str) and 0 < len(item.strip()) <= MAX_CONSTRAINT for item in items):
        raise DelegationError(f"constraints must be at most {MAX_CONSTRAINTS} short non-empty strings")
    return [item.strip() for item in items]


def _goal(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_GOAL:
        raise DelegationError(f"goal must be a non-empty string of at most {MAX_GOAL} characters")
    return value.strip()


def _deadline(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DelegationError("deadline must be a positive epoch-seconds integer")
    return value


def effective_authority(
    delegate_grants: Iterable[str], delegator_authority: Iterable[str], originator_permissions: Iterable[str]
) -> frozenset[str]:
    """The Tools a delegate may use for a task: only what all three parties allow."""
    return _tools(delegate_grants, "delegate grants") & _tools(delegator_authority, "delegator authority") \
        & _tools(originator_permissions, "originating principal permissions")


def root_envelope(
    *,
    task_id: str,
    agent: str,
    originating_principal: str,
    goal: str,
    agent_grants: Iterable[str],
    originator_permissions: Iterable[str],
    budget: Mapping[str, int] | None = None,
    deadline: int | None = None,
    constraints: Iterable[str] | None = None,
    required_capabilities: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The first envelope of a task: the requester's authority, narrowed to what the first Agent holds."""
    authority = _tools(agent_grants, "agent grants") & _tools(originator_permissions, "originating principal permissions")
    required = _tools(required_capabilities, "required capabilities")
    if not required <= authority:
        raise DelegationError("the task needs Tools its first Agent and requester do not both hold: " + ", ".join(sorted(required - authority)))
    return {
        "taskId": _task_id(task_id), "parentTaskId": None, "agent": _name(agent, "agent"),
        "originatingPrincipal": _name(originating_principal, "originating principal"),
        "goal": _goal(goal), "constraints": _constraints(constraints),
        "requiredCapabilities": sorted(required), "authority": sorted(authority),
        "deadline": _deadline(deadline), "budget": _budget(budget, "budget"), "spent": {},
        "depth": 0, "delegationChain": [_name(agent, "agent")],
    }


def remaining(envelope: Mapping[str, Any]) -> dict[str, int]:
    budget, spent = envelope.get("budget", {}), envelope.get("spent", {})
    return {key: max(0, amount - int(spent.get(key, 0))) for key, amount in budget.items()}


def exhausted(envelope: Mapping[str, Any]) -> list[str]:
    """Budget dimensions with nothing left; a task with any exhausted bound must halt (and its owner be told)."""
    return sorted(key for key, left in remaining(envelope).items() if left == 0)


def charge(envelope: Mapping[str, Any], **used: int) -> dict[str, Any]:
    """A copy of the envelope with usage recorded. Using more than is left is allowed to be recorded but halts the task."""
    spent = dict(envelope.get("spent", {}))
    for key, amount in _budget(used, "usage").items():
        spent[key] = spent.get(key, 0) + amount
    return {**envelope, "spent": spent}


def delegate(
    parent: Mapping[str, Any],
    *,
    to_agent: str,
    child_task_id: str,
    goal: str,
    delegate_grants: Iterable[str],
    originator_permissions: Iterable[str],
    required_capabilities: Iterable[str] | None = None,
    budget: Mapping[str, int] | None = None,
    deadline: int | None = None,
    constraints: Iterable[str] | None = None,
    children_so_far: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_fan_out: int = DEFAULT_MAX_FAN_OUT,
    now: int | None = None,
) -> dict[str, Any]:
    """The child envelope for handing part of ``parent``'s task to ``to_agent``, or ``DelegationError``."""
    _limits(max_depth, max_fan_out)
    delegate_name = _name(to_agent, "delegate agent")
    chain = [str(item) for item in parent.get("delegationChain", [])]
    if delegate_name in chain:
        raise DelegationError(f"delegation loop: {delegate_name} is already in the chain {' -> '.join(chain)}")
    depth = int(parent.get("depth", 0)) + 1
    if depth > max_depth:
        raise DelegationError(f"delegation would be {depth} deep; the limit is {max_depth}")
    if not isinstance(children_so_far, int) or isinstance(children_so_far, bool) or children_so_far < 0:
        raise DelegationError("children_so_far must be a non-negative integer")
    if children_so_far >= max_fan_out:
        raise DelegationError(f"{parent.get('agent')} already delegated to {children_so_far} Agents; the limit is {max_fan_out}")
    left = remaining(parent)
    if exhausted(parent):
        raise DelegationError("the parent task's budget is exhausted: " + ", ".join(exhausted(parent)))
    parent_deadline = parent.get("deadline")
    if now is not None and parent_deadline is not None and now >= parent_deadline:
        raise DelegationError("the parent task's deadline has passed")
    authority = effective_authority(delegate_grants, parent.get("authority", []), originator_permissions)
    required = _tools(required_capabilities, "required capabilities")
    if not required <= authority:
        raise DelegationError(
            f"{delegate_name} cannot be given {', '.join(sorted(required - authority))}: it is outside what the delegate, "
            "the delegator and the originating principal all allow"
        )
    requested = _budget(budget, "budget")
    child_budget: dict[str, int] = {}
    for key in sorted(set(left) | set(requested)):
        if key in left:
            wanted = requested.get(key, left[key])
            if wanted > left[key]:
                raise DelegationError(f"budget.{key} {wanted} is more than the parent has left ({left[key]})")
            child_budget[key] = wanted
        else:
            child_budget[key] = requested[key]
    child_deadline = _deadline(deadline)
    if parent_deadline is not None:
        if child_deadline is not None and child_deadline > parent_deadline:
            raise DelegationError("the delegated deadline is later than the parent's")
        child_deadline = child_deadline or parent_deadline
    merged_constraints = [*list(parent.get("constraints", [])), *_constraints(constraints)]
    if len(merged_constraints) > 2 * MAX_CONSTRAINTS:
        raise DelegationError("too many accumulated constraints for one task")
    return {
        "taskId": _task_id(child_task_id, "child task id"), "parentTaskId": parent["taskId"], "agent": delegate_name,
        "originatingPrincipal": parent["originatingPrincipal"],  # fixed by the root; a child cannot change it
        "goal": _goal(goal), "constraints": merged_constraints,
        "requiredCapabilities": sorted(required), "authority": sorted(authority),
        "deadline": child_deadline, "budget": child_budget, "spent": {},
        "depth": depth, "delegationChain": [*chain, delegate_name],
    }


def governance_context(envelope: Mapping[str, Any], *, parent_request_id: str | None = None) -> dict[str, Any]:
    """The bounded governance context fields a call made under this envelope must carry."""
    context: dict[str, Any] = {
        "originatingPrincipal": envelope["originatingPrincipal"],
        "taskId": envelope["taskId"],
        "delegationChain": list(envelope["delegationChain"]),
    }
    if parent_request_id:
        context["parentRequestId"] = parent_request_id
    return context


def permits(envelope: Mapping[str, Any], tool_name: str) -> bool:
    """Whether a Tool call is inside the task's authority; a runtime must check this before policy evaluation."""
    return tool_name in set(envelope.get("authority", []))


def hop_details(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """Ledger details for one delegation hop: identifiers, counts and a digest of the goal, never the goal itself."""
    return {
        "fromTask": parent["taskId"], "toTask": child["taskId"], "fromAgent": parent["agent"], "toAgent": child["agent"],
        "depth": child["depth"], "authorityTools": len(child["authority"]),
        "goalDigest": hashlib.sha256(child["goal"].encode("utf-8")).hexdigest(),
        "chain": list(child["delegationChain"]),
    }


def record_hop(govern: Any, audit_path: pathlib.Path | None, parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """Ledger the hop through ``govern.record_operator_action`` (``scripts/govern-action.py``) under the delegator's name."""
    return govern.record_operator_action(audit_path, "delegation.hop", f"agent:{parent['agent']}", hop_details(parent, child))


def canonical(envelope: Mapping[str, Any]) -> str:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))
