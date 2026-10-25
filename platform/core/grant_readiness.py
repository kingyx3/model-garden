"""Skill readiness that checks the coworker's *own* provider grant, not just that a provider is bound.

An environment binding saying "calendar is Google" says nothing about whether *this* coworker may use it: another
coworker's grant, a revoked or expired grant, or a grant that does not cover the Tools the Agent selected would all leave
a Skill that cannot work shown as ready (#111, #129). This module overlays the per-coworker grant registry
(``provider_grants.py``) on a capability manifest so a connected Skill is only ready when its Agent holds a usable grant of its
own for the bound provider **and capability family** that covers the Tools it selected. It reads only the non-secret public view of grants (never a
secret reference), holds no credential and changes no grant.

- **Only connected providers.** A Tier 0 dependency needs no credential and is untouched, as is a dependency with no bound
  provider. Every other bound dependency is checked against grants held by *this manifest's Agent*; another coworker's grant
  never counts.
- **Three new blocking states.** ``no-grant`` (the Agent has no grant for the bound provider), ``grant-unavailable`` (every
  such grant is revoked, expired or failed) and ``grant-scope`` (the usable grant does not cover every Tool the Agent selected
  in that family; the Tool names are listed). A required dependency in one of these states blocks the Skill; an optional one
  degrades to its fallback tier. Each blocked Skill carries the one action that unblocks it.
- **Family-level scopes.** The manifest records the Tools the Agent selected per family, not per Skill, so the scope check is
  per family: a Skill that needs only ``calendar.availability`` is still blocked if the grant lacks ``calendar.book`` that the
  Agent also selected. This errs toward blocking and the message names the missing Tools.
- **A healthy grant adds a non-secret note.** ``grant: {state: active|degraded, isolation}``; no identity, connection or secret.
- **Optional by deployment.** The sidecar applies the overlay only when a grant registry exists for the environment, so
  single-Agent workspaces without one behave exactly as before until their operator provisions grants
  (``provider_grants.upgrade_from_environment`` shows what that looks like). A registry that exists but cannot be read blocks
  connected Skills (fail closed) rather than being ignored.

Not done: provisioning grants, resolving them for a Tool call in the sidecar (it still uses the environment binding), or
checking the provider's own scopes live.
"""
from __future__ import annotations

import copy
import importlib.util
import pathlib
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

REGISTRY_DIR = "provider-grants"
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _sibling(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _capabilities():
    return _sibling("model_garden_grant_readiness_capabilities", "capabilities.py")


def tenant_of(manifest: Mapping[str, Any]) -> str | None:
    """The registry tenant slug (``<client>-<environment>``) the manifest belongs to, or ``None`` when it does not say."""
    client, environment = manifest.get("client"), manifest.get("environment")
    if not isinstance(client, str) or not isinstance(environment, str):
        return None
    slug = f"{client}-{environment}".lower()
    return slug if _TENANT.match(slug) else None


def _usable(grant: Mapping[str, Any], now: int) -> bool:
    expires = grant.get("expiresAt")
    return not grant.get("revokedAt") and grant.get("health") != "failed" and not (isinstance(expires, int) and expires <= now)


def _verdict(
    grants: Iterable[Mapping[str, Any]], *, agent: Any, family: Any, provider: Any, tools: Iterable[str], now: int
) -> tuple[str, dict[str, Any]]:
    """``(status, extra)`` for one connected dependency: ``bound`` with a grant note, or a blocking grant state."""
    held = [item for item in grants if item.get("agent") == agent and item.get("family") == family and item.get("provider") == provider]
    if not held:
        return "no-grant", {}
    usable = [item for item in held if _usable(item, now)]
    if not usable:
        return "grant-unavailable", {}
    wanted = sorted(set(tools))
    gaps = [([tool for tool in wanted if tool not in grant.get("scopes", [])], grant) for grant in usable]
    missing, best = min(gaps, key=lambda pair: len(pair[0]))
    if missing:
        return "grant-scope", {"missingTools": missing}
    return "bound", {"grant": {"state": "degraded" if best.get("health") == "degraded" else "active", "isolation": best.get("isolation")}}


def overlay(
    manifest: Mapping[str, Any],
    grants: Iterable[Mapping[str, Any]],
    *,
    now: int,
    registry: str = "readable",
) -> dict[str, Any]:
    """A copy of ``manifest`` with each connected Skill dependency checked against this Agent's own grants.

    ``grants`` is the registry's public view (``GrantRegistry.list``). ``registry="unreadable"`` means the registry exists
    but could not be read: every connected dependency is then treated as having no grant.
    """
    capabilities = _capabilities()
    updated = copy.deepcopy(dict(manifest))
    agent = updated.get("agent")
    held = [dict(item) for item in grants] if registry == "readable" else []
    selected = {entry.get("family"): entry.get("tools", []) for entry in updated.get("capabilities", []) if isinstance(entry, dict)}
    unavailable_families: set[str] = set()
    for skill in updated.get("skills", []):
        for dependency in skill.get("dependencies", []):
            if dependency.get("status") != "bound" or not isinstance(dependency.get("tier"), int) or dependency["tier"] == 0:
                continue
            family = dependency.get("family")
            status, extra = _verdict(
                held, agent=agent, family=family, provider=dependency.get("provider"), tools=selected.get(family, []), now=now
            )
            if status == "bound":
                dependency.update(extra)
                continue
            if status != "grant-scope":
                unavailable_families.add(family)
            if dependency.get("required"):
                dependency["status"] = status
            else:
                dependency["status"] = "degraded"
                dependency["fallbackTier"] = capabilities.families()[family]["fallbackTier"]
                dependency["grantIssue"] = status
            if "missingTools" in extra:
                dependency["missingTools"] = extra["missingTools"]
        skill["ready"] = all(dep.get("status") not in capabilities.BLOCKING for dep in skill.get("dependencies", []))
        if skill.get("reason"):
            skill["ready"], skill["canEnable"] = False, False
        else:
            skill["canEnable"] = skill["ready"]
            fix = None if skill["ready"] else capabilities.remediation_for(skill.get("dependencies", []))
            if fix:
                skill["remediation"] = fix
            else:
                skill.pop("remediation", None)
    for entry in updated.get("capabilities", []):
        if not isinstance(entry, dict) or entry.get("family") not in unavailable_families or "provider" not in entry:
            continue
        entry["status"] = "unavailable"
        entry["grant"] = "unavailable"
        entry["fallback"] = capabilities._fallback(capabilities.families()[entry["family"]]["fallbackTier"])
    if registry != "readable":
        updated["grantRegistry"] = registry
    return updated


def overlay_from_root(manifest: Mapping[str, Any], root: pathlib.Path, *, now: int) -> dict[str, Any]:
    """Apply the overlay when a grant registry exists for the manifest's environment; otherwise return the manifest unchanged.

    The registry is only read when its file exists, so this never creates files; one that exists but is unreadable or belongs
    to another tenant is treated as holding no grants, so connected Skills block instead of falling back to a shared token.
    """
    tenant = tenant_of(manifest)
    registry_file = pathlib.Path(root) / f"{tenant}.grants.json" if tenant else None
    if registry_file is None or not registry_file.is_file():
        return copy.deepcopy(dict(manifest))
    grants_module = _sibling("model_garden_grant_readiness_grants", "provider_grants.py")
    try:
        grants = grants_module.GrantRegistry(pathlib.Path(root), tenant=tenant).list(agent=manifest.get("agent"))
    except (grants_module.GrantError, OSError, ValueError):
        return overlay(manifest, [], now=now, registry="unreadable")
    return overlay(manifest, grants, now=now)
