"""The owner-readable onboarding pack: what the coworker can do, what it needs next and what it will not do without asking (#111, #114).

The compiler already computes two things for a client environment: per-Skill readiness (which Skills work, which are blocked and
the one action that unblocks each) and the approvals preview (which Tool actions run on their own, which wait for a person and which
are never done). Both are written for an operator. A business owner deciding what to switch on needs the same facts in plain words,
in one place, that they can read, keep and hand to their consultant. This module turns the two reports into that pack. It is a pure
function over already-computed, non-secret reports: it reads no workspace, calls no provider and holds no credential.

- **Plain language, same facts.** Nothing is added that the reports do not already say; this only rewords it and groups it by what
  the owner can do next. A Skill is *working*, *needs one thing from you* or *switched off*.
- **The one next step.** A blocked Skill shows what it is missing and the next operator action without claiming that a preview authorizes access. When the missing piece is a
  capability family the pack gives the coworker-bound ``connect`` discovery command and the options with their honest status: a provider that is only planned
  is listed as not usable yet, never as available.
- **What happens without asking.** For each Skill's Tools the pack states, for people on the team and for the public, whether the action
  runs by itself, waits for approval, runs by itself only inside the signed limits, is handed to a named person or is never done.
  Public callers can never approve; approvals come from a mapped approver, never from the caller.
- **Non-secret by construction.** The pack lists names, capability families, tiers, risk labels and commands. It never includes a
  secret reference, connection id, account, scope or policy rule content, and the renderer refuses text that looks like a credential.
- **Not a promise of live operation.** The pack says what the *configuration* allows. It does not claim a provider is connected live,
  and it says so at the foot.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

BUILT = {"live", "built-credential-free"}
_AGENT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_CAPABILITY_FAMILY = re.compile(r"^[a-z]+$")
_CONNECTION_BLOCKERS = {"missing", "unavailable", "no-grant", "grant-unavailable", "grant-scope"}
_SECRETISH = re.compile(r"secret://|token|credential|password|passwd|api[-_]?key|private[-_]?key|bearer ", re.IGNORECASE)
RUNS = {
    "automatic": "runs by itself",
    "approval": "waits for a person's approval",
    "conditional": "runs by itself only inside the signed limits, otherwise waits for approval",
    "human": "is handed to a named person",
    "denied": "is never done",
}
Options = Callable[[str], Iterable[Mapping[str, Any]]]


class PackError(ValueError):
    """Raised when a report is malformed or the pack would carry something that looks like a credential."""


def _dependency(item: Mapping[str, Any]) -> str:
    label = str(item["capability"]) + ("" if item.get("required", True) else " (optional)")
    status = item["status"]
    if status == "bound":
        return f"{label}: provided by {item.get('provider')} (tier {item.get('tier')})"
    if status == "missing":
        return f"{label}: not connected yet"
    if status == "no-content":
        return f"{label}: connected, but no approved business knowledge is selected for this coworker"
    if status == "unavailable":
        return f"{label}: the connection failed or was revoked and needs the operator to reauthorize it"
    if status == "no-grant":
        return f"{label}: connected, but this coworker has no access of its own yet"
    if status == "grant-unavailable":
        return f"{label}: this coworker's access was revoked, expired or failed"
    if status == "grant-scope":
        return f"{label}: this coworker's access does not cover everything the Skill needs"
    if status == "degraded":
        return f"{label}: not connected; a simpler fallback (tier {item.get('fallbackTier')}) is used instead"
    raise PackError(f"unknown dependency status {status!r}")


def _connect_options(family: str, options: Options | None) -> list[dict[str, Any]]:
    if options is None:
        return []
    return [
        {"id": str(choice["id"]), "tier": choice.get("tier"), "usable": choice.get("status") in BUILT,
         "status": str(choice.get("status"))}
        for choice in options(family)
    ]


def build(
    readiness: Iterable[Mapping[str, Any]],
    approvals: Iterable[Mapping[str, Any]],
    *,
    options: Options | None = None,
) -> list[dict[str, Any]]:
    """One pack per Agent from the readiness and approvals reports (as produced by ``compile-workspace.py``)."""
    # A missing or duplicated policy preview must not turn into "No actions are selected yet".
    # The owner must see the approval decisions for exactly the Agents whose readiness
    # was evaluated, not a partial or stale report from a different selection.
    readiness_reports = list(readiness)
    approval_reports = list(approvals)
    readiness_by_agent: dict[str, Mapping[str, Any]] = {}
    approvals_by_agent: dict[str, Mapping[str, Any]] = {}
    for entry in readiness_reports:
        name = entry["agent"]
        if not isinstance(name, str) or not _AGENT_NAME.fullmatch(name):
            raise PackError("invalid readiness Agent identity for owner guidance")
        if name in readiness_by_agent:
            raise PackError(f"duplicate readiness Agent {name!r}")
        readiness_by_agent[name] = entry
    for entry in approval_reports:
        name = entry["agent"]
        if not isinstance(name, str) or not _AGENT_NAME.fullmatch(name):
            raise PackError("invalid approval Agent identity for owner guidance")
        if name in approvals_by_agent:
            raise PackError(f"duplicate approval Agent {name!r}")
        if not isinstance(entry.get("policyVersion"), str) or not entry["policyVersion"]:
            raise PackError(f"missing policyVersion for Agent {name!r}")
        approvals_by_agent[name] = entry
    if set(readiness_by_agent) != set(approvals_by_agent):
        raise PackError("approval report must cover exactly the readiness Agents")

    packs: list[dict[str, Any]] = []
    for agent in readiness_reports:
        name = agent["agent"]
        seen_skills: set[tuple[str, str]] = set()
        working: list[dict[str, Any]] = []
        needs: list[dict[str, Any]] = []
        off: list[str] = []
        for entry in agent["skills"]:
            key = (entry.get("kind", "Skill"), entry["name"])
            if key in seen_skills:
                raise PackError(f"duplicate Skill {key!r} for Agent {name!r}")
            seen_skills.add(key)
            if entry.get("enabled", True) and entry["ready"] and any(
                dependency.get("required", True) and dependency["status"] in
                {"missing", "no-content", "unavailable", "no-grant", "grant-unavailable", "grant-scope"}
                for dependency in entry["dependencies"]
            ):
                raise PackError(f"Skill {entry['name']!r} claims ready with a required unavailable dependency")
            label = {"name": entry["name"], "kind": entry.get("kind", "Skill")}
            if not entry.get("enabled", True):
                off.append(entry["name"])
            elif entry["ready"]:
                working.append({**label, "uses": [_dependency(item) for item in entry["dependencies"]]})
            else:
                blocked = [item for item in entry["dependencies"] if item["status"] != "bound" and item.get("required", True)]
                if not blocked:
                    raise PackError(f"blocked Skill {entry['name']!r} has no required blocking dependency")
                families = sorted({item["family"] for item in blocked if item["status"] in _CONNECTION_BLOCKERS})
                if any(not isinstance(family, str) or not _CAPABILITY_FAMILY.fullmatch(family) for family in families):
                    raise PackError("invalid capability family for coworker connection guidance")
                needs.append({
                    **label,
                    "missing": [_dependency(item) for item in blocked],
                    "action": entry.get("remediation"),
                    "connect": [
                        {"family": family, "command": f"python3 scripts/client-operator.py connect {family} --agent {name}",
                         "options": _connect_options(family, options)}
                        for family in families
                    ],
                })
        policy = approvals_by_agent[name]
        actions = [
            {"skill": group["skill"], "tool": action["tool"], "risk": action["risk"], "team": action["member"], "public": action["public"]}
            for group in policy["skills"]
            for action in group["actions"]
        ]
        packs.append({
            "agent": name,
            "environment": agent.get("environment"),
            "policyVersion": policy["policyVersion"],
            "working": working,
            "needsAction": needs,
            "switchedOff": sorted(off),
            "actions": actions,
        })
    return packs


def _check(text: str) -> str:
    if _SECRETISH.search(text):
        raise PackError("the onboarding pack would contain something that looks like a credential")
    return text


def render(packs: Iterable[Mapping[str, Any]]) -> str:
    """Markdown the business owner can read, keep and send to their consultant."""
    lines: list[str] = []
    for pack in packs:
        where = pack.get("environment") or "this environment"
        lines += [f"# Your AI coworker: {pack['agent']} ({where})", ""]
        lines += ["## What it can do now", ""]
        if pack["working"]:
            for skill in pack["working"]:
                lines.append(f"- **{skill['name']}**" + (f" — {'; '.join(skill['uses'])}" if skill["uses"] else ""))
        else:
            lines.append("- Nothing is ready yet. See what is needed below.")
        lines += ["", "## What needs one thing from you first", ""]
        if pack["needsAction"]:
            for skill in pack["needsAction"]:
                lines.append(f"- **{skill['name']}** cannot work yet: {'; '.join(skill['missing']) or 'a dependency is not ready'}.")
                if skill["action"]:
                    lines.append(f"  - Next step: {skill['action']}")
                for family in skill["connect"]:
                    lines.append(f"  - Review choices for this coworker: `{family['command']}` (does not grant or repair access).")
                    usable = [choice for choice in family["options"] if choice["usable"]]
                    if usable:
                        lines.append("  - Ready to use: " + ", ".join(f"{choice['id']} (tier {choice['tier']})" for choice in usable))
                    else:
                        lines.append("  - No usable connector for this is shipped yet; your consultant can tell you the plan.")
            lines.append("  - A consultant must review the selected provider, exact Tools and owner/operator identities before any separate consent, health test and activation. This pack cannot authorize access.")
        else:
            lines.append("- Nothing is blocked.")
        if pack["switchedOff"]:
            lines += ["", "## Switched off on purpose", ""]
            lines += [f"- {name}: listed for review but not active. Your consultant enables it after the setup it needs and a test." for name in pack["switchedOff"]]
        lines += ["", "## What it will and will not do without asking you", ""]
        if pack["actions"]:
            if pack["policyVersion"]:
                lines += [f"Approval policy version {pack['policyVersion']}.", ""]
            lines += ["| Action | Used by | Risk | Team members | Public callers |", "| --- | --- | --- | --- | --- |"]
            for action in pack["actions"]:
                used = action["skill"] or "no active Skill"
                lines.append(f"| {action['tool']} | {used} | {action['risk']} | {RUNS[action['team']]} | {RUNS[action['public']]} |")
            lines += [
                "",
                "Approvals come from a person on your approver list, never from the caller. A public caller can never approve, change the policy or reach a privileged action.",
            ]
        else:
            lines.append("No actions are selected yet.")
        lines += [
            "",
            "_This describes what the configuration allows. It does not show that any outside account is connected or that a live call has been tested._",
            "",
        ]
    return _check("\n".join(lines))
