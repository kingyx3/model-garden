"""Provider-neutral capability contracts, resolution and the per-environment manifest.

Agents and Skills ask for capability families (``mail``, ``calendar``) or portable
catalog Tools (``calendar.book``), never for a vendor. Only the EnvironmentBinding
chooses a provider. This module owns three small, deterministic jobs:

- validate declarations and Tool resources against ``contracts/v1/capabilities.json``;
- resolve declarations against one EnvironmentBinding, failing closed for unbound
  required capabilities (naming the Skills that need them) and reporting unbound
  optional ones as degraded with their fallback tier, overall and per Skill;
- generate the non-secret capability manifest an environment exposes to its coworker
  and overlay live connection health without redeploying Agents.
"""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import sys
from typing import Any, Mapping

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "contracts" / "v1" / "capabilities.json"
INTEGRATIONS_PATH = ROOT / "contracts" / "v1" / "integrations.json"
IMPLEMENTED_STATUSES = {"live", "built-credential-free"}
MANIFEST_SCHEMA_VERSION = 1
TIERS = (0, 1, 2, 3, 4)
HUMAN_HANDOFF_TIER = 4
CONNECTION_HEALTH = ("healthy", "degraded", "failed", "revoked")
UNAVAILABLE_HEALTH = {"failed", "revoked"}
BLOCKING = {"missing", "no-content", "unavailable", "no-grant", "grant-unavailable", "grant-scope"}
SAFE_SCOPE = re.compile(r"^[A-Za-z0-9._:/+-]{1,200}$")
SAFE_NAME = re.compile(r"^[a-z]+$")

_CATALOG: dict[str, Any] | None = None


def load_catalog(path: pathlib.Path | None = None) -> dict[str, Any]:
    global _CATALOG
    if path is not None:
        return _read_catalog(path)
    if _CATALOG is None:
        _CATALOG = _read_catalog(CATALOG_PATH)
    return _CATALOG


def _read_catalog(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("kind") != "CapabilityCatalog" or not isinstance(value.get("families"), dict):
        raise ValueError(f"{path}: not a Model Garden CapabilityCatalog")
    return value


def families(catalog: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    return (catalog or load_catalog())["families"]


def catalog_tool(name: str, catalog: dict[str, Any] | None = None) -> dict[str, Any] | None:
    family = name.split(".", 1)[0]
    entry = families(catalog).get(family)
    if entry is None:
        return None
    return entry["tools"].get(name)


def family_of(name: str, catalog: dict[str, Any] | None = None) -> str | None:
    """Return the family a declaration names, or None when the catalog does not know it."""
    known = families(catalog)
    if name in known:
        return name
    if catalog_tool(name, catalog) is not None:
        return name.split(".", 1)[0]
    return None


def ledger_fields(tool_name: str, catalog: dict[str, Any] | None = None) -> frozenset[str]:
    """Argument keys the catalog marks safe to keep verbatim in the action ledger."""
    tool = catalog_tool(tool_name, catalog)
    return frozenset(tool.get("ledgerFields", ())) if tool else frozenset()


def tool_risk(tool_name: str, catalog: dict[str, Any] | None = None) -> str | None:
    tool = catalog_tool(tool_name, catalog)
    return tool.get("risk") if tool else None


def tool_contract_errors(tool: dict[str, Any], catalog: dict[str, Any] | None = None) -> list[str]:
    """Check a Tool resource that opts in to the capability contract with spec.capability."""
    name = tool.get("metadata", {}).get("name")
    spec = tool.get("spec", {})
    capability = spec.get("capability")
    if capability is None:
        return []
    if capability not in families(catalog):
        return [f"Tool {name!r} declares unknown capability family {capability!r}"]
    contract = families(catalog)[capability]["tools"].get(name)
    if contract is None:
        return [f"Tool {name!r} is not a portable {capability!r} Tool in the capability catalog"]
    if spec.get("risk") != contract["risk"]:
        return [f"Tool {name!r} risk {spec.get('risk')!r} does not match catalog risk {contract['risk']!r}"]
    return []


def collect_declarations(agent: dict[str, Any], skills: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Union Agent and selected Skill declarations; required always wins over optional."""
    required: set[str] = set()
    optional: set[str] = set()
    for resource in [agent, *skills]:
        spec = resource.get("spec", {})
        required.update(spec.get("requiredCapabilities", []))
        optional.update(spec.get("optionalCapabilities", []))
    return {"required": sorted(required), "optional": sorted(optional - required)}


def declaration_errors(declarations: dict[str, list[str]], owner: str, catalog: dict[str, Any] | None = None) -> list[str]:
    errors = []
    for name in [*declarations.get("required", []), *declarations.get("optional", [])]:
        if family_of(name, catalog) is None:
            errors.append(f"{owner} declares unknown capability {name!r}")
    return errors


def _binding_spec(binding: Mapping[str, Any]) -> Mapping[str, Any]:
    if binding.get("kind") == "EnvironmentBinding":
        spec = binding.get("spec")
        if not isinstance(spec, Mapping):
            raise ValueError("EnvironmentBinding.spec must be an object")
        return spec
    return binding


def binding_environment(binding: Mapping[str, Any]) -> str | None:
    value = _binding_spec(binding).get("environment")
    return value if isinstance(value, str) and value else None



def _implemented_connector(family: str, provider: str, tier: int) -> bool:
    """Only shipped Tier 0 implementations or verified adapters satisfy a dependency.

    The registry is an implementation inventory, not proof of live OAuth consent.
    Bindings name a calendar provider ``google`` or ``microsoft``; the registry ids carry a ``-calendar`` suffix.
    """
    try:
        registry = json.loads(INTEGRATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Integration registry missing or invalid; refuse capability activation") from exc
    identifier = (
        {"google": "google-calendar", "microsoft": "microsoft-calendar"}.get(provider, provider)
        if family == "calendar" else provider
    )
    for entry in registry.get("providers", []):
        if entry.get("family") != family or entry.get("id") != identifier:
            continue
        if entry.get("status") not in IMPLEMENTED_STATUSES or entry.get("tier") != tier:
            return False
        adapter = entry.get("adapter")
        return isinstance(adapter, str) and (ROOT / adapter).is_file()
    return False


def bound_providers(binding: Mapping[str, Any], catalog: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return {family: non-secret connection facts} declared by an EnvironmentBinding.

    ``connectors.<family>.provider`` is the only shape. Only allow-listed, non-secret
    fields are returned.
    """
    connectors = _binding_spec(binding).get("connectors")
    if connectors is None:
        return {}
    if not isinstance(connectors, Mapping):
        raise ValueError("EnvironmentBinding connectors must be an object")
    known = families(catalog)
    bound: dict[str, dict[str, Any]] = {}
    declared_families: set[str] = set()
    for key, value in connectors.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"EnvironmentBinding connectors.{key} must be an object")
        if key in known:
            family, provider = key, value.get("provider")
        else:
            continue
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"EnvironmentBinding connectors.{key}.provider must be a non-empty string")
        if family in declared_families:
            raise ValueError(f"EnvironmentBinding binds more than one provider for capability {family!r}")
        declared_families.add(family)
        tier = value.get("tier", 1)
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in TIERS:
            raise ValueError(f"EnvironmentBinding connectors.{key}.tier must be an integer 0-4")
        facts: dict[str, Any] = {"provider": provider, "tier": tier}
        scopes = value.get("scopes")
        if scopes is not None:
            if not isinstance(scopes, list) or not all(isinstance(s, str) and SAFE_SCOPE.fullmatch(s) for s in scopes):
                raise ValueError(f"EnvironmentBinding connectors.{key}.scopes must be a list of scope names")
            facts["scopes"] = sorted(set(scopes))
        owner = value.get("owner")
        if owner is not None:
            if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
                raise ValueError(f"EnvironmentBinding connectors.{key}.owner must be a non-empty string")
            facts["owner"] = owner.strip()
        if _implemented_connector(family, provider, tier):
            bound[family] = facts
        # A declared but unshipped provider does not satisfy capability readiness.
    return bound


def resolve(
    declarations: dict[str, list[str]],
    binding: Mapping[str, Any],
    *,
    agent_name: str,
    catalog: dict[str, Any] | None = None,
    required_by: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Resolve declarations against one environment; fail closed on unbound required ones.

    ``required_by`` maps a required declaration to the Skills that require it, so the
    fail-closed message can name them (see ``skills_requiring``).
    """
    providers = bound_providers(binding, catalog)
    environment = binding_environment(binding)
    resolution: list[dict[str, Any]] = []
    missing: list[str] = []
    for required, names in ((True, declarations.get("required", [])), (False, declarations.get("optional", []))):
        for name in names:
            family = family_of(name, catalog)
            if family is None:
                raise ValueError(f"Agent {agent_name!r} declares unknown capability {name!r}")
            entry: dict[str, Any] = {"capability": name, "family": family, "required": required}
            facts = providers.get(family)
            if facts is not None:
                entry.update(status="bound", provider=facts["provider"], tier=facts["tier"])
            elif required:
                missing.append(name)
                continue
            else:
                entry.update(status="degraded", fallbackTier=families(catalog)[family]["fallbackTier"])
            resolution.append(entry)
    if missing:
        where = f"environment {environment!r}" if environment else "the target environment"
        needed = ", ".join(_with_requirers(name, (required_by or {}).get(name, [])) for name in missing)
        raise ValueError(
            f"Agent {agent_name!r} requires capability {needed} "
            f"but {where} binds no provider for it; add connectors.<family>.provider to the "
            "EnvironmentBinding or make the capability optional"
        )
    resolution.sort(key=lambda item: (not item["required"], item["capability"]))
    return {
        "environment": environment,
        "resolution": resolution,
        "degraded": [item["capability"] for item in resolution if item["status"] == "degraded"],
    }


def _with_requirers(name: str, skills: list[str]) -> str:
    if not skills:
        return repr(name)
    noun = "Skill" if len(skills) == 1 else "Skills"
    return f"{name!r} (needed by {noun} {', '.join(repr(skill) for skill in sorted(skills))})"


def skills_requiring(skills: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Return {required declaration: sorted Skill names that require it}."""
    owners: dict[str, set[str]] = {}
    for skill in skills:
        skill_name = skill.get("metadata", {}).get("name")
        for name in skill.get("spec", {}).get("requiredCapabilities", []):
            owners.setdefault(name, set()).add(skill_name)
    return {name: sorted(names) for name, names in sorted(owners.items())}


def knowledge_states(
    sources: list[Mapping[str, Any]], *, agent: str | None, today: dt.date | None = None
) -> list[dict[str, str]]:
    """Non-secret ``[{name, state}]`` for the KnowledgeSources an Agent selects (see knowledge.source_state)."""
    module_name = "model_garden_capabilities_knowledge"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, pathlib.Path(__file__).with_name("knowledge.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return [
        {"name": source.get("metadata", {}).get("name"),
         "state": module.source_state(source.get("spec", {}), agent=agent, today=today)}
        for source in sources if isinstance(source, Mapping)
    ]


def remediation_for(dependencies: list[Mapping[str, Any]], *, plain: bool = False) -> str | None:
    """The one non-secret action that unblocks a Skill, or ``None`` when nothing blocks it.

    Names the first blocking dependency (by capability name) and never includes a secret, scope,
    account or source content: only the family, the command or the kind of approval needed.
    ``plain`` words the two steps that name an operator shell command for a business owner instead (ask the operator or
    consultant to do it), which is what the coworker passes on in chat.
    """
    for dependency in sorted(
        (item for item in dependencies if item.get("status") in BLOCKING),
        key=lambda item: (str(item.get("capability")), str(item.get("status"))),
    ):
        family, status = dependency.get("family"), dependency.get("status")
        if status == "missing":
            if plain:
                return f"Connect {family}: ask your operator or consultant to start the guided connect step for it"
            return f"Connect {family}: run `python3 scripts/client-operator.py connect {family}` to see the options"
        if status == "no-content":
            if any(source.get("state") == "stale" for source in dependency.get("content", {}).get("sources", [])):
                return (
                    "Review the business knowledge: confirm it is still correct and set a later reviewBy date, "
                    "then recompile"
                )
            if any(source.get("state") == "restricted" for source in dependency.get("content", {}).get("sources", [])):
                return (
                    "A knowledge source is classified restricted, which the built-in store does not serve: connect a knowledge "
                    "provider with document-level permissions, or lower the classification after review, then recompile"
                )
            return (
                "Approve business knowledge for this coworker: a source with status approved, a current "
                "effective date and this coworker as audience, then recompile"
            )
        if status == "unavailable":
            return f"Ask the operator to reauthorize the {family} connection, then record it as healthy"
        if status == "no-grant":
            if plain:
                return (
                    f"Give this coworker its own {family} access: ask your operator to provision a grant for it "
                    "(another coworker's access cannot be used)"
                )
            return (
                f"Give this coworker its own {family} access: run `python3 scripts/client-operator.py connect {family}` "
                "and provision a grant for it (another coworker's access cannot be used)"
            )
        if status == "grant-unavailable":
            return f"Ask the operator to renew or replace this coworker's {family} grant, which is revoked, expired or failed"
        if status == "grant-scope":
            tools = ", ".join(dependency.get("missingTools", []))
            return f"Ask the operator to re-scope this coworker's {family} grant to include: {tools}" if tools else (
                f"Ask the operator to re-scope this coworker's {family} grant to cover the Tools it selected"
            )
    return None


def skill_readiness(
    agent: dict[str, Any],
    skills: list[dict[str, Any]],
    binding: Mapping[str, Any],
    catalog: dict[str, Any] | None = None,
    *,
    disabled_skills: set[str] | None = None,
    knowledge: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Report, per selected Skill, whether its capability dependencies are met here.

    One entry per Skill, preceded by one for the Agent when it declares capabilities
    itself. A dependency is ``bound`` (provider and tier), ``missing`` (required and
    unbound: the entry is not ready) or ``degraded`` (optional and unbound, with its
    fallback tier). Only provider, tier and fallback tier are copied from the binding:
    never token references, calendar IDs, scopes or owners.

    When ``knowledge`` (see :func:`knowledge_states`) is given, a bound ``kb`` dependency also
    reports the state of the business content behind the provider: a *required* one with no
    usable (approved, effective, audience-matching) source is ``no-content``, which blocks the
    Skill exactly like a missing provider would, because a coworker with a knowledge store but
    nothing approved to say cannot answer from it.
    """
    providers = bound_providers(binding, catalog)
    disabled_skills = disabled_skills or set()
    owners: list[tuple[str, dict[str, Any]]] = []
    agent_spec = agent.get("spec", {})
    if agent_spec.get("requiredCapabilities") or agent_spec.get("optionalCapabilities"):
        owners.append(("Agent", agent))
    owners.extend(("Skill", skill) for skill in skills)
    report: list[dict[str, Any]] = []
    for kind, resource in owners:
        name = resource.get("metadata", {}).get("name")
        spec = resource.get("spec", {})
        required = sorted(set(spec.get("requiredCapabilities", [])))
        optional = sorted(set(spec.get("optionalCapabilities", [])) - set(required))
        dependencies: list[dict[str, Any]] = []
        for is_required, names in ((True, required), (False, optional)):
            for capability in names:
                family = family_of(capability, catalog)
                if family is None:
                    raise ValueError(f"{kind} {name!r} declares unknown capability {capability!r}")
                dependency: dict[str, Any] = {"capability": capability, "family": family, "required": is_required}
                facts = providers.get(family)
                if facts is not None:
                    dependency.update(status="bound", provider=facts["provider"], tier=facts["tier"])
                    if knowledge is not None and family == "kb":
                        usable = sum(1 for item in knowledge if item["state"] == "usable")
                        dependency["content"] = {"usable": usable, "sources": [dict(item) for item in knowledge]}
                        if is_required and usable == 0:
                            dependency["status"] = "no-content"
                elif is_required:
                    dependency["status"] = "missing"
                else:
                    dependency.update(status="degraded", fallbackTier=families(catalog)[family]["fallbackTier"])
                dependencies.append(dependency)
        ready = all(item["status"] not in BLOCKING for item in dependencies)
        entry: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "enabled": kind != "Skill" or name not in disabled_skills,
            "ready": ready,
            "canEnable": ready,
            "dependencies": dependencies,
        }
        if not ready:
            entry["remediation"] = remediation_for(dependencies)
        report.append(entry)
    return report


def build_manifest(desired_state: dict[str, Any], binding: Mapping[str, Any], catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate the truthful, non-secret "what can the coworker do here" manifest.

    The manifest lists every catalog family so the coworker can also say what it
    cannot do and which fallback applies. It carries no token references, calendar
    IDs, mailboxes or tenant identifiers beyond the client and environment name.
    """
    catalog = catalog or load_catalog()
    providers = bound_providers(binding, catalog)
    source = desired_state.get("source", {}) if isinstance(desired_state.get("source"), dict) else {}
    selected = {
        tool.get("metadata", {}).get("name")
        for tool in desired_state.get("tools", [])
        if isinstance(tool, dict)
    }
    declared = desired_state.get("capabilities", {}) if isinstance(desired_state.get("capabilities"), dict) else {}
    required = set(declared.get("required", []))
    optional = set(declared.get("optional", []))
    entries = []
    for family in sorted(families(catalog)):
        contract = families(catalog)[family]
        tools = sorted(name for name in contract["tools"] if name in selected)
        entry: dict[str, Any] = {
            "family": family,
            "tools": tools,
            "required": any(family_of(name, catalog) == family for name in required),
            "optional": any(family_of(name, catalog) == family for name in optional),
        }
        facts = providers.get(family)
        if facts is None:
            entry.update(status="unbound", tier=contract["fallbackTier"], fallback=_fallback(contract["fallbackTier"]))
        else:
            entry.update(
                status="available" if tools else "not-granted",
                provider=facts["provider"],
                tier=facts["tier"],
                scopes=facts.get("scopes", []),
                connectionOwner=facts.get("owner"),
                health="unknown",
            )
            if not tools:
                entry["fallback"] = _fallback(contract["fallbackTier"])
        entries.append(entry)
    # Runtime binding is the authority for readiness. Compilation may have been
    # performed without one, or with a different development environment.
    agent = desired_state.get("agent")
    if isinstance(agent, dict):
        disabled = set(agent.get("spec", {}).get("disabledSkills", []))
        executable_skills = [
            skill for skill in desired_state.get("skills", []) if isinstance(skill, dict)
        ]
        # Disabled Skills are intentionally absent from executable skills and
        # Hermes instructions. Keep only their non-executable declarations
        # available for an accurate client-facing readiness explanation.
        disabled_declarations = [
            skill for skill in desired_state.get("disabledSkillDeclarations", [])
            if isinstance(skill, dict) and skill.get("metadata", {}).get("name") in disabled
        ]
        known = {skill.get("metadata", {}).get("name") for skill in [*executable_skills, *disabled_declarations]}
        selected_knowledge = desired_state.get("knowledge")
        skill_status = skill_readiness(
            agent, [*executable_skills, *disabled_declarations], binding, catalog,
            disabled_skills=disabled,
            knowledge=(
                knowledge_states(selected_knowledge, agent=source.get("name"))
                if isinstance(selected_knowledge, list) else None
            ),
        )
        # A disabled Skill the profile carries no declaration for must not
        # look safe to switch on without recompilation.
        unknown = disabled - known
        for name in sorted(unknown):
            skill_status.append({
                "name": name, "kind": "Skill", "enabled": False, "ready": False,
                "canEnable": False, "dependencies": [],
                "reason": "Recompile the workspace to check this disabled Skill's dependencies",
            })
    else:
        skill_status = declared.get("skills", [])

    manifest: dict[str, Any] = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "kind": "CapabilityManifest",
        "catalogVersion": catalog["catalogVersion"],
        "agent": source.get("name"),
        "environment": binding_environment(binding),
        "capabilities": entries,
        "skills": skill_status,
    }
    if isinstance(source.get("client"), str):
        manifest["client"] = source["client"]
    return manifest


def _fallback(tier: int) -> str:
    return "human-handoff" if tier == HUMAN_HANDOFF_TIER else "built-in"


def _health_dir(root: pathlib.Path) -> pathlib.Path:
    return root / "connections"


def record_connection_health(
    root: pathlib.Path,
    family: str,
    health: str,
    *,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist one connection's live health; the manifest picks it up on the next read."""
    if not SAFE_NAME.fullmatch(family) or family not in families():
        raise ValueError(f"unknown capability family {family!r}")
    if health not in CONNECTION_HEALTH:
        raise ValueError(f"connection health must be one of {', '.join(CONNECTION_HEALTH)}")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("connection health actor must be non-empty")
    record: dict[str, Any] = {
        "family": family,
        "health": health,
        "updatedBy": actor.strip(),
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if reason:
        record["reason"] = reason[:200]
    directory = _health_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{family}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return record


def read_connection_health(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    directory = _health_dir(root)
    if not directory.is_dir():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("family") == path.stem and value.get("health") in CONNECTION_HEALTH:
            records[path.stem] = value
    return records


def apply_connection_health(manifest: dict[str, Any], health: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Overlay live health; failed or revoked connections become unavailable with a fallback."""
    updated = json.loads(json.dumps(manifest))
    unavailable = set()
    for entry in updated.get("capabilities", []):
        record = health.get(entry.get("family"))
        if record is None or "provider" not in entry:
            continue
        entry["health"] = record["health"]
        entry["healthUpdatedAt"] = record.get("updatedAt")
        if record["health"] in UNAVAILABLE_HEALTH:
            entry["status"] = "unavailable"
            entry["fallback"] = _fallback(families()[entry["family"]]["fallbackTier"])
            unavailable.add(entry["family"])

    # A static deploy-time readiness result must not remain "ready" when
    # authorization fails or a client owner revokes the connection. Never
    # copy runtime health records, identities or reasons into the Skill view.
    for skill in updated.get("skills", []):
        for dependency in skill.get("dependencies", []):
            family = dependency.get("family")
            if family not in unavailable or dependency.get("status") != "bound":
                continue
            if dependency.get("required"):
                dependency["status"] = "unavailable"
            else:
                dependency["status"] = "degraded"
                dependency["fallbackTier"] = families()[family]["fallbackTier"]
        skill["ready"] = all(
            dep.get("status") not in BLOCKING
            for dep in skill.get("dependencies", [])
        )
        # A disabled Skill with no declaration cannot be activated until a
        # new compilation restores its requirements.
        if skill.get("reason"):
            skill["ready"] = False
            skill["canEnable"] = False
        else:
            skill["canEnable"] = skill["ready"]
            fix = None if skill["ready"] else remediation_for(skill.get("dependencies", []))
            if fix:
                skill["remediation"] = fix
            else:
                skill.pop("remediation", None)
    return updated


PUBLIC_DEPENDENCY_KEYS = ("capability", "family", "required", "status", "fallbackTier")
PUBLIC_CAPABILITY_KEYS = ("family", "status", "fallback")


def caller_view(manifest: Mapping[str, Any], *, member: bool) -> dict[str, Any]:
    """What ``platform_capabilities`` tells the coworker, narrowed to who is asking.

    The full manifest carries operator wording (shell commands to connect a provider), provider names, grant state and the names and
    states of knowledge sources. None of that is for a customer:

    - A verified team member (``member``) gets each blocked Skill's one ``nextStep`` in plain words (ask the operator or consultant;
      never a command) and the rest of the manifest as it is, without the operator ``remediation`` text.
    - Anyone else (a public caller, an unmapped sender, a call with no attributed person) gets only whether each Skill is ready and
      the capability family and status, so the coworker can truthfully say "I can't book appointments here" and nothing about how
      this client's systems are set up or which sources it holds.
    """
    view = copy.deepcopy(dict(manifest))
    for skill in view.get("skills", []):
        skill.pop("remediation", None)
        if member:
            step = None if skill.get("ready") else remediation_for(skill.get("dependencies", []), plain=True)
            if step:
                skill["nextStep"] = step
        else:
            skill["dependencies"] = [{key: item[key] for key in PUBLIC_DEPENDENCY_KEYS if key in item} for item in skill.get("dependencies", [])]
            for key in ("reason", "canEnable"):
                skill.pop(key, None)
    if not member:
        view["capabilities"] = [{key: item[key] for key in PUBLIC_CAPABILITY_KEYS if key in item} for item in view.get("capabilities", [])]
        view.pop("environment", None)
    return view
