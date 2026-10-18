"""A team of Agents in one environment: per-Agent Tool isolation and per-Agent provider access.

The compiler can already emit desired state for several Agents (``compile-workspace.py`` without ``--agent``), but each
governed sidecar serves one Agent (#95). This module is the credential-free check a multi-Agent runtime has to pass
before it may advertise more than one coworker. It reads compiled desired states and, optionally, the per-coworker
grant registry (``provider_grants.py``, #129); it changes nothing; ``platform/runtime/team_runtime.py`` refuses to start a team that fails it.

- **Tool isolation.** ``authorize_call`` allows a call only if the Tool is one of the *calling* Agent's own compiled
  Tools. The Agent name must be the runtime-attested one; another Agent's Tool is refused even though it exists in the
  same environment.
- **Own provider access.** With several Agents, every Agent whose Tools belong to an external (tier 1+) connector family
  needs a usable grant of its own covering those Tools. An Agent with none is ``blocked`` with a plain reason; nothing
  falls back to the environment-level token. Tier 0 families (built into the platform) need no grant.
- **Single-Agent workspaces are unchanged.** One Agent and no registry is ``ready`` exactly as before, using the
  environment binding, and the plan says so.
- **Unique identity.** Duplicate Agent names are refused, because the Agent name is what attribution, approvals and
  ledger records are bound to.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any



class TeamError(PermissionError):
    """Raised for an invalid team or a call outside the calling Agent's own Tools."""


def agent_name(state: Mapping[str, Any]) -> str:
    name = state.get("agent", {}).get("metadata", {}).get("name")
    if not isinstance(name, str) or not name:
        raise TeamError("a desired state needs an Agent name")
    return name


def tool_names(state: Mapping[str, Any]) -> list[str]:
    return sorted(tool["metadata"]["name"] for tool in state.get("tools", []))


def family_of(tool: str) -> str:
    return tool.split(".", 1)[0]


def _index(states: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for state in states:
        name = agent_name(state)
        if name in indexed:
            raise TeamError(f"two Agents are named {name}; an Agent name identifies its approvals, attribution and ledger records")
        indexed[name] = state
    if not indexed:
        raise TeamError("a team needs at least one Agent")
    return indexed


def _binding(environment_binding: Mapping[str, Any] | None, family: str) -> Mapping[str, Any] | None:
    connectors = (environment_binding or {}).get("connectors") or {}
    entry = connectors.get(family)
    return entry if isinstance(entry, Mapping) else None


def authorize_call(states: Sequence[Mapping[str, Any]], attested_agent: str, tool: str) -> bool:
    """True when ``tool`` is one of the attested Agent's own compiled Tools; otherwise ``TeamError``."""
    indexed = _index(states)
    state = indexed.get(attested_agent)
    if state is None:
        raise TeamError(f"{attested_agent!r} is not an Agent in this environment")
    if tool not in tool_names(state):
        raise TeamError(f"{tool} is not one of {attested_agent}'s Tools")
    return True


def _usable(grant: Mapping[str, Any], now: int) -> bool:
    if grant.get("revokedAt") or grant.get("health") == "failed":
        return False
    expires = grant.get("expiresAt")
    return expires is None or expires > now


def plan(
    states: Sequence[Mapping[str, Any]],
    *,
    environment_binding: Mapping[str, Any] | None = None,
    registry: Any | None = None,
    now: int,
) -> dict[str, Any]:
    """Per-Agent readiness: Tools, connector families and whether each Agent has the provider access of its own it needs."""
    indexed = _index(states)
    multi = len(indexed) > 1
    agents: list[dict[str, Any]] = []
    for name in sorted(indexed):
        tools = tool_names(indexed[name])
        families: dict[str, dict[str, Any]] = {}
        for family in sorted({family_of(tool) for tool in tools}):
            entry = _binding(environment_binding, family)
            tier = int(entry.get("tier", 1)) if entry else None
            wanted = [tool for tool in tools if family_of(tool) == family]
            info: dict[str, Any] = {"tools": wanted, "provider": entry.get("provider") if entry else None, "tier": tier}
            if entry is None:
                info.update(status="unbound", reason=f"{family} is not bound in this environment")
            elif tier == 0:
                info.update(status="built-in", reason="a platform Tier 0 provider needs no external grant")
            elif not multi and registry is None:
                info.update(status="environment-binding", reason="single-Agent workspace: uses the environment binding, unchanged")
            else:
                grants = [grant for grant in (registry.list(agent=name) if registry is not None else []) if grant["family"] == family and _usable(grant, now)]
                covered = {scope for grant in grants for scope in grant["scopes"]}
                missing = [tool for tool in wanted if tool not in covered]
                if not grants:
                    info.update(status="blocked", reason=f"{name} has no usable {family} grant of its own", missingTools=wanted)
                elif missing:
                    info.update(status="blocked", reason=f"{name}'s {family} grant does not cover {', '.join(missing)}", missingTools=missing)
                else:
                    unsupported = [grant["id"] for grant in grants if not grant["multiCoworkerSupported"]]
                    if multi and unsupported:
                        info.update(status="blocked", reason=f"{name}'s {family} access is a shared login, which is unsupported for several coworkers",
                                    missingTools=wanted)
                    else:
                        info.update(status="granted", grantIds=sorted(grant["id"] for grant in grants))
            families[family] = info
        blocked = [family for family, info in families.items() if info["status"] in ("blocked", "unbound")]
        agents.append({
            "agent": name, "client": indexed[name].get("source", {}).get("client"), "tools": tools, "families": families,
            "ready": not blocked, "blockedFamilies": blocked,
        })
    return {"mode": "multi-agent" if multi else "single-agent", "agents": agents, "ready": all(item["ready"] for item in agents)}


def blocked_reasons(report: Mapping[str, Any]) -> list[str]:
    return [
        f"{item['agent']}: {item['families'][family]['reason']}"
        for item in report["agents"] for family in item["blockedFamilies"]
    ]
