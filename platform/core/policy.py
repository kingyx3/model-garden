"""Client-owned approval policy for the governed action boundary.

A Policy resource holds ordered rules (`allow`, `deny`, `require-approval`, `human-only`)
over Tool names, data classifications, channels and principal types. Evaluation is
deliberately small and fail-closed:

- a matching `deny` rule always wins;
- a matching `human-only` rule forbids automation: the action is prepared for the rule's named
  assignee (Tier 4 hand-off) instead of being executed, and no approval can turn it into execution;
- the Agent's ``approvals.externalActions: required`` switch and any matching
  `require-approval` rule can never be relaxed by an `allow` rule (the stricter source wins);
- an `allow` rule may waive only a Tool's own ``approvalRequired`` default, and only when it
  carries a valid client-owner sign-off bound to the rule's exact content;
- an `allow` rule applies to member principals unless it names ``public`` explicitly, and a
  public principal can reach at most one signed, named ``write`` Tool;
- ``privileged`` Tools are never auto-approved and never reachable by a public principal;
- an `allow` rule may carry bounded ``conditions`` (an argument allow-list and a per-day usage
  limit). Conditions only narrow a loosening rule: when one is unmet, or cannot be checked,
  the rule simply does not apply and the stricter default stands. They are part of the rule's
  content, so the owner's sign-off digest binds them too.

With no Policy the result is exactly the previous Agent/Tool behaviour, plus the privileged
floor. This module owns no I/O and no credentials.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

EFFECTS = ("allow", "deny", "require-approval", "human-only")
MEMBER = "member"
PUBLIC = "public"
DEFAULT_VERSION = "model-garden-governance/1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rule_digest(rule: Mapping[str, Any]) -> str:
    """Digest of a rule's content excluding its sign-off, so a signature binds what it approves."""
    return hashlib.sha256(_canonical({k: v for k, v in rule.items() if k != "signoff"}).encode("utf-8")).hexdigest()


def rules_of(policies: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten Policy resources into one ordered rule list (policy order, then rule order)."""
    flattened: list[dict[str, Any]] = []
    for policy in policies or []:
        for rule in policy.get("spec", {}).get("rules", []):
            flattened.append(dict(rule))
    return flattened


def policy_version(policies: Iterable[Mapping[str, Any]] | None) -> str:
    """Stable identifier of the effective rules; the legacy constant when there are none."""
    rules = rules_of(policies)
    if not rules:
        return DEFAULT_VERSION
    return "policy:" + hashlib.sha256(_canonical(rules).encode("utf-8")).hexdigest()[:16]


def signoff_valid(rule: Mapping[str, Any]) -> bool:
    signoff = rule.get("signoff")
    return (
        isinstance(signoff, Mapping)
        and isinstance(signoff.get("owner"), str)
        and bool(signoff["owner"].strip())
        and signoff.get("ruleDigest") == rule_digest(rule)
    )


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def conditions_status(
    rule: Mapping[str, Any],
    arguments: Mapping[str, Any] | None,
    usage_today: int | None,
) -> str | None:
    """None when a rule's bounded conditions hold, else why the rule does not apply.

    Fails closed: an argument the call did not supply, a non-scalar value, or a usage count the
    caller could not provide all make the condition unmet.
    """
    conditions = rule.get("conditions")
    if not isinstance(conditions, Mapping):
        return None
    allowed = conditions.get("arguments")
    if isinstance(allowed, Mapping):
        supplied = arguments if isinstance(arguments, Mapping) else {}
        for name, values in allowed.items():
            value = supplied.get(name)
            if not _scalar(value) or not any(type(value) is type(item) and value == item for item in values):
                return "argument-condition"
    limit = conditions.get("maxPerDay")
    if limit is not None and (usage_today is None or usage_today >= limit):
        return "usage-limit"
    return None


def _action_matches(rule: Mapping[str, Any], tool_name: str) -> bool:
    patterns = rule.get("actions") or []
    return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns)


def _matches(
    rule: Mapping[str, Any],
    *,
    tool_name: str,
    classification: str | None,
    channel: str | None,
    principal: str,
) -> bool:
    effect = rule.get("effect")
    restrictive = effect != "allow"
    if not _action_matches(rule, tool_name) and not (restrictive and not rule.get("actions")):
        return False
    classes = rule.get("dataClassifications") or []
    if classes and (classification not in classes if classification is not None else not restrictive):
        return False
    channels = rule.get("channels") or []
    if channels and (channel not in channels if channel is not None else not restrictive):
        return False
    principals = rule.get("principals") or []
    if principals:
        return principal in principals
    # A restrictive rule covers every principal; a loosening rule covers members only.
    return restrictive or principal == MEMBER


def evaluate(
    policies: Iterable[Mapping[str, Any]] | None,
    *,
    tool_name: str,
    risk: str | None,
    classification: str | None = None,
    channel: str | None = None,
    principal: str | None = None,
    agent_requires_approval: bool = False,
    tool_requires_approval: bool = False,
    arguments: Mapping[str, Any] | None = None,
    usage_today: int | None = None,
) -> dict[str, Any]:
    """Return ``{"decision", "reason", "ruleId"}`` for one already-selected Tool call.

    ``arguments`` and ``usage_today`` (successful executions of this Tool so far today, UTC) feed
    the bounded conditions of `allow` rules; omit them and conditional rules do not apply.
    """
    who = principal or MEMBER
    matched = []
    limited: dict[str, Any] | None = None  # first allow rule set aside only because its daily limit is used up
    for rule in rules_of(policies):
        if not _matches(rule, tool_name=tool_name, classification=classification, channel=channel, principal=who):
            continue
        if rule["effect"] == "allow":
            unmet = conditions_status(rule, arguments, usage_today)
            if unmet is not None:
                if unmet == "usage-limit" and usage_today is not None and limited is None:
                    limited = rule
                continue
        matched.append(rule)
    denied = next((rule for rule in matched if rule["effect"] == "deny"), None)
    if denied is not None:
        return {"decision": "deny", "reason": "policy-deny", "ruleId": denied["id"]}
    allows = [rule for rule in matched if rule["effect"] == "allow"]
    waiver = next((rule for rule in allows if signoff_valid(rule)), None)

    if who == PUBLIC and risk != "read":
        # Floor: a public principal never reaches privileged or external-communication
        # Tools, and reaches a write Tool only through an explicit, signed rule.
        if risk != "write" or waiver is None or PUBLIC not in (waiver.get("principals") or []):
            return {"decision": "deny", "reason": "principal-not-authorized", "ruleId": None}

    human_only = next((rule for rule in matched if rule["effect"] == "human-only"), None)
    if human_only is not None:
        # Automation is forbidden for this action: prepare it for a named person instead.
        return {
            "decision": "hand-off",
            "reason": "policy-human-only",
            "ruleId": human_only["id"],
            "assignee": human_only.get("assignee"),
            "deadlineHours": human_only.get("deadlineHours"),
        }
    if agent_requires_approval:
        return {"decision": "require-approval", "reason": "agent-requires-approval", "ruleId": None}
    required = next((rule for rule in matched if rule["effect"] == "require-approval"), None)
    if required is not None:
        return {"decision": "require-approval", "reason": "policy-require-approval", "ruleId": required["id"]}
    if risk == "privileged":
        return {"decision": "require-approval", "reason": "privileged-requires-approval", "ruleId": None}
    if tool_requires_approval and waiver is None:
        if limited is not None:
            return {"decision": "require-approval", "reason": "policy-limit-reached", "ruleId": limited["id"]}
        return {"decision": "require-approval", "reason": "tool-requires-approval", "ruleId": None}
    if waiver is not None:
        return {"decision": "allow", "reason": "policy-allow", "ruleId": waiver["id"]}
    if allows:
        return {"decision": "allow", "reason": "policy-allow", "ruleId": allows[0]["id"]}
    return {"decision": "allow", "reason": "selected-tool", "ruleId": None}


def relaxation_errors(policies: Iterable[Mapping[str, Any]] | None, tools: Iterable[Mapping[str, Any]]) -> list[str]:
    """Deploy-time checks: rule ids are unique, and every approval-relaxing rule is signed.

    An `allow` rule relaxes approval when it matches a selected Tool that is not read-class.
    Its sign-off must name the client owner and carry the digest of the rule's exact content,
    so editing a rule after it was approved blocks compilation until the owner re-signs.
    """
    errors: list[str] = []
    seen: set[str] = set()
    selected = [
        (tool.get("metadata", {}).get("name", ""), tool.get("spec", {}).get("risk"))
        for tool in tools
    ]
    for rule in rules_of(policies):
        rule_id = rule["id"]
        if rule_id in seen:
            errors.append(f"Policy rule id {rule_id!r} is defined more than once")
        seen.add(rule_id)
        if rule["effect"] == "human-only" and not (isinstance(rule.get("assignee"), str) and rule["assignee"].strip()):
            errors.append(f"Policy human-only rule {rule_id!r} must name the person (assignee) who performs the action")
        if rule["effect"] != "allow":
            if rule.get("conditions") is not None:
                errors.append(
                    f"Policy rule {rule_id!r} has conditions but is not an allow rule; "
                    "conditions may only narrow a loosening rule"
                )
            continue
        if not rule.get("actions"):
            errors.append(f"Policy allow rule {rule_id!r} must list the actions it allows")
            continue
        relaxes = any(risk != "read" and _action_matches(rule, name) for name, risk in selected)
        if PUBLIC in (rule.get("principals") or []):
            relaxes = True
            risky = [
                name for name, risk in selected
                if _action_matches(rule, name) and risk not in ("read", "write")
            ]
            if risky:
                errors.append(
                    f"Policy rule {rule_id!r} would let a public principal reach "
                    f"{', '.join(sorted(risky))}; only read and write Tools may be named for public principals"
                )
        if relaxes and not signoff_valid(rule):
            errors.append(
                f"Policy rule {rule_id!r} relaxes an approval requirement and needs the client owner's "
                f"sign-off: signoff.owner plus signoff.ruleDigest {rule_digest(rule)}"
            )
    return errors


def diff_policies(
    before: Iterable[Mapping[str, Any]] | None, after: Iterable[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Describe a policy change as rule ids, flagging the ones that loosen control."""
    old = {rule["id"]: rule for rule in rules_of(before)}
    new = {rule["id"]: rule for rule in rules_of(after)}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(
        rule_id for rule_id in set(old) & set(new)
        if _canonical(old[rule_id]) != _canonical(new[rule_id])
    )
    relaxed = sorted(
        {rule_id for rule_id in added + modified if new[rule_id]["effect"] == "allow"}
        | {rule_id for rule_id in removed + modified if old[rule_id]["effect"] != "allow"}
    )
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "relaxed": relaxed,
        "previousVersion": policy_version(before),
        "newVersion": policy_version(after),
    }


def preview(
    policies: Iterable[Mapping[str, Any]] | None,
    *,
    tool_name: str,
    risk: str | None,
    classification: str | None = None,
    agent_requires_approval: bool = False,
    tool_requires_approval: bool = False,
) -> dict[str, str]:
    """Outcome per principal type for the readiness/onboarding preview (channel-agnostic)."""
    outcome: dict[str, str] = {}
    for who in (MEMBER, PUBLIC):
        result = evaluate(
            policies,
            tool_name=tool_name,
            risk=risk,
            classification=classification,
            principal=who,
            agent_requires_approval=agent_requires_approval,
            tool_requires_approval=tool_requires_approval,
        )
        outcome[who] = {
            "allow": "automatic", "require-approval": "approval", "deny": "denied", "hand-off": "human",
        }[result["decision"]]
        if outcome[who] == "approval" and any(
            rule.get("effect") == "allow" and rule.get("conditions") and _action_matches(rule, tool_name)
            and (who in (rule.get("principals") or [MEMBER]))
            for rule in rules_of(policies)
        ):
            # Some calls (matching arguments, inside the daily limit) run automatically; others still need approval.
            outcome[who] = "conditional"
    return outcome
