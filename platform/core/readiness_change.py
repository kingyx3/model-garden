"""What a connection change would do to Skill readiness, before anything is written (#110, #111).

The guided connect step (``connect.py``) produces a binding fragment (provider, tier, scopes, owner). Before an owner consents, and
again after the binding changes, they should see the effect in the terms they care about: which Skills start working, which stay
blocked and why, and whether anything that worked would stop. This compares two per-Skill readiness reports (as produced by
``compile-workspace.py``) and says exactly that. It is a pure function over already-computed, non-secret reports: it writes no
binding, calls no provider and holds no credential, and a Skill whose provider is not shipped or whose grant is missing correctly
stays blocked, so the preview never claims more than the readiness rules allow.

- **Four outcomes per Skill.** ``unblocked`` (blocked or switched off before, ready now), ``regressed`` (ready before, not now: a
  change that would break something is shown, not hidden), ``still-blocked`` (with the one next step) and ``unchanged``.
- **A switched-off Skill stays switched off.** Connecting a provider never enables a Skill the owner disabled; it is reported as
  ``switched-off`` so the next step is the explicit enable, not a surprise.
- **Absent is not ready.** A Skill that appears on only one side is reported ``added`` or ``removed`` and counts as a regression only
  if it was ready before and is gone.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

OUTCOMES = ("unblocked", "regressed", "still-blocked", "switched-off", "unchanged", "added", "removed")


def _state(entry: Mapping[str, Any]) -> str:
    if not entry.get("enabled", True):
        return "off"
    return "ready" if entry.get("ready") else "blocked"


def diff(before: Iterable[Mapping[str, Any]], after: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One result per Agent: its Skills grouped by what the change does to them."""
    later = {item["agent"]: item for item in after}
    results: list[dict[str, Any]] = []
    for agent in before:
        name = agent["agent"]
        if name not in later:
            raise ValueError(f"Agent {name!r} is missing from the after report")
        old = {(entry.get("kind", "Skill"), entry["name"]): entry for entry in agent["skills"]}
        new = {(entry.get("kind", "Skill"), entry["name"]): entry for entry in later[name]["skills"]}
        rows: list[dict[str, Any]] = []
        for key in sorted(set(old) | set(new), key=lambda item: (item[0], item[1])):
            kind, skill = key
            if key not in old:
                outcome = "added"
            elif key not in new:
                outcome = "removed"
            else:
                was, now = _state(old[key]), _state(new[key])
                if now == "ready":
                    outcome = "unchanged" if was == "ready" else "unblocked"
                elif was == "ready":
                    outcome = "regressed"
                elif now == "off":
                    outcome = "switched-off"
                else:
                    outcome = "still-blocked"
            row = {"name": skill, "kind": kind, "outcome": outcome}
            if outcome in ("still-blocked", "regressed") and key in new and new[key].get("remediation"):
                row["next"] = new[key]["remediation"]
            rows.append(row)
        results.append({
            "agent": name,
            "environment": later[name].get("environment"),
            "changes": rows,
            "regressed": any(row["outcome"] == "regressed" or (row["outcome"] == "removed" and _state(old[(row["kind"], row["name"])]) == "ready") for row in rows),
        })
    return results


def render(changes: Iterable[Mapping[str, Any]], *, subject: str = "this change") -> str:
    """Plain words for the owner: what works after, what still needs a step, and any warning first."""
    lines: list[str] = []
    for agent in changes:
        lines.append(f"Agent {agent['agent']}: what {subject} would do")
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in agent["changes"]:
            grouped.setdefault(row["outcome"], []).append(row)
        if agent["regressed"]:
            lines.append("  WARNING: this would stop something that works today:")
            for row in grouped.get("regressed", []) + grouped.get("removed", []):
                lines.append(f"    - {row['name']}" + (f" — next step: {row['next']}" if row.get("next") else ""))
        for outcome, title in (("unblocked", "Starts working"), ("still-blocked", "Still needs a step"), ("switched-off", "Stays switched off until you enable it"),
                               ("added", "Newly listed"), ("unchanged", "Already working, unchanged")):
            rows = grouped.get(outcome, [])
            if not rows:
                continue
            lines.append(f"  {title}:")
            for row in rows:
                lines.append(f"    - {row['name']}" + (f" — next step: {row['next']}" if row.get("next") else ""))
    return "\n".join(lines) + "\n"
