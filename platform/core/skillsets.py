"""Skillsets: reusable, versioned bundles of Skill references, with per-coworker activation.

A Skillset lets a client choose a package ("front office") instead of picking Skills one by one, without that
choice granting anything (#128). It is a catalogue bundle of *references* to Skills: not a security principal, not
an Agent, not a unit that is switched on or off as a whole, and not a place for credentials. This module is the
credential-free first contract. It validates a bundle, expands a coworker's selection deterministically, and keeps
**selection separate from activation**. It is not yet read by ``compile-workspace.py`` or any registry; it produces
the ``skills`` and ``disabledSkills`` values the compiler already understands.

- **Validation is strict.** Unknown keys are refused, and so is any key that looks like a secret.
- **Expansion fails closed.** A missing Skillset, a missing Skill, a Skillset that pins a Skill version that is not the
  one available, two Skillsets that pin the same Skill differently, and a Skillset that includes itself (a cycle, or
  nesting deeper than 4) each raise ``SkillsetError``; nothing is partly expanded.
- **Selection is not activation.** Every selected Skill has a coworker-specific state. By default nothing is
  enabled; enabling one member leaves every other member of the same bundle off, and a Tool is exposed only through
  an enabled Skill that declares it, so a bundle can never add a Tool, credential or authority.
- **No coworker state inside the bundle.** The same Skillset resolves independently for two coworkers, each with its
  own available Skills and its own enabled set.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

MAX_DEPTH = 4
MAX_SKILLS_PER_SET = 100
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_TOP_KEYS = {"name", "version", "description", "provenance", "skills", "includes"}
_SKILL_KEYS = {"name", "version"}
_PROVENANCE_KEYS = {"source", "pin"}
_SECRETISH = re.compile(r"secret|token|credential|password|passwd|api[-_]?key|private[-_]?key", re.IGNORECASE)


class SkillsetError(ValueError):
    """Raised for an invalid bundle, an unresolvable selection or an unsafe activation; nothing is partly applied."""


def _check_keys(mapping: Mapping[str, Any], allowed: set[str], where: str) -> None:
    for key in mapping:
        if _SECRETISH.search(str(key)):
            raise SkillsetError(f"{where}: a Skillset is not a place for credentials ({key!r})")
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise SkillsetError(f"{where}: unsupported keys: {', '.join(unknown)}")


def validate_skillset(document: Any) -> dict[str, Any]:
    """The normalised bundle, or ``SkillsetError``. Members are references (name, optional exact version pin)."""
    if not isinstance(document, Mapping):
        raise SkillsetError("a Skillset is an object")
    _check_keys(document, _TOP_KEYS, "skillset")
    name = document.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise SkillsetError("skillset name must be lowercase letters, digits and hyphens")
    version = document.get("version")
    if not isinstance(version, str) or not _VERSION.match(version):
        raise SkillsetError(f"skillset {name}: version must look like 1.2.0")
    description = document.get("description", "")
    if not isinstance(description, str) or len(description) > 500:
        raise SkillsetError(f"skillset {name}: description is at most 500 characters")
    provenance = document.get("provenance")
    if provenance is not None:
        if not isinstance(provenance, Mapping):
            raise SkillsetError(f"skillset {name}: provenance is an object")
        _check_keys(provenance, _PROVENANCE_KEYS, f"skillset {name} provenance")
        if not all(isinstance(value, str) and 0 < len(value) <= 200 for value in provenance.values()):
            raise SkillsetError(f"skillset {name}: provenance values are short strings")
    members = document.get("skills", [])
    if not isinstance(members, Sequence) or isinstance(members, (str, bytes)) or len(members) > MAX_SKILLS_PER_SET:
        raise SkillsetError(f"skillset {name}: skills is a list of at most {MAX_SKILLS_PER_SET} references")
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in members:
        reference = {"name": member} if isinstance(member, str) else member
        if not isinstance(reference, Mapping):
            raise SkillsetError(f"skillset {name}: a Skill reference is a name or {{name, version}}")
        _check_keys(reference, _SKILL_KEYS, f"skillset {name} skill")
        skill = reference.get("name")
        pin = reference.get("version")
        if not isinstance(skill, str) or not _NAME.match(skill):
            raise SkillsetError(f"skillset {name}: a Skill name is lowercase letters, digits and hyphens")
        if pin is not None and (not isinstance(pin, str) or not _VERSION.match(pin)):
            raise SkillsetError(f"skillset {name}: Skill {skill} pin must look like 1.2.0")
        if skill in seen:
            raise SkillsetError(f"skillset {name}: Skill {skill} is listed twice")
        seen.add(skill)
        skills.append({"name": skill, **({"version": pin} if pin else {})})
    includes = document.get("includes", [])
    if not isinstance(includes, Sequence) or isinstance(includes, (str, bytes)) or not all(isinstance(item, str) and _NAME.match(item) for item in includes):
        raise SkillsetError(f"skillset {name}: includes is a list of Skillset names")
    if len(set(includes)) != len(includes):
        raise SkillsetError(f"skillset {name}: includes lists a Skillset twice")
    if not skills and not includes:
        raise SkillsetError(f"skillset {name}: a Skillset needs at least one Skill or include")
    return {"name": name, "version": version, "description": description, "skills": skills, "includes": list(includes),
            **({"provenance": dict(provenance)} if provenance else {})}


def _catalog(catalog: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for name, versions in catalog.items():
        if not isinstance(versions, Mapping):
            raise SkillsetError(f"catalog entry {name} maps versions to Skillsets")
        for version, document in versions.items():
            normal = validate_skillset(document)
            if normal["name"] != name or normal["version"] != version:
                raise SkillsetError(f"catalog entry {name}@{version} does not match its document")
            result.setdefault(name, {})[version] = normal
    return result


def resolve(
    selection: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
    available_skills: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Expand a coworker's selection into a deterministic, deduplicated list of Skills.

    ``selection`` is ``{"skills": [name, ...], "skillsets": [{"name", "version"}, ...]}`` (either may be empty).
    ``catalog`` maps Skillset name -> version -> bundle (pinned shared libraries plus client-approved entries).
    ``available_skills`` maps each Skill name this coworker's workspace provides to its version. Each result row is
    ``{"skill", "version", "via"}`` where ``via`` lists ``direct`` and/or the Skillsets that brought it, sorted by name.
    """
    if not isinstance(selection, Mapping):
        raise SkillsetError("a selection is an object")
    _check_keys(selection, {"skills", "skillsets"}, "selection")
    library = _catalog(catalog)
    direct = list(selection.get("skills", []))
    chosen = list(selection.get("skillsets", []))
    if not all(isinstance(item, str) and _NAME.match(item) for item in direct):
        raise SkillsetError("selected Skills are names")
    via: dict[str, set[str]] = {}
    pins: dict[str, tuple[str, str]] = {}

    def add(skill: str, source: str, pin: str | None, owner: str) -> None:
        if skill not in available_skills:
            raise SkillsetError(f"Skill {skill} (from {owner}) is not available to this coworker")
        if pin is not None and available_skills[skill] != pin:
            raise SkillsetError(f"{owner} pins Skill {skill}@{pin} but {available_skills[skill]} is available")
        if pin is not None:
            if skill in pins and pins[skill][0] != pin:
                raise SkillsetError(f"conflicting pins for Skill {skill}: {pins[skill][1]} wants {pins[skill][0]}, {owner} wants {pin}")
            pins[skill] = (pin, owner)
        via.setdefault(skill, set()).add(source)

    def visit(name: str, version: str | None, trail: tuple[str, ...]) -> None:
        if name in trail:
            raise SkillsetError("Skillset cycle: " + " -> ".join((*trail, name)))
        if len(trail) >= MAX_DEPTH:
            raise SkillsetError(f"Skillsets are nested at most {MAX_DEPTH} deep")
        versions = library.get(name)
        if not versions:
            raise SkillsetError(f"Skillset {name} is not in the catalog")
        if version is None or version not in versions:
            raise SkillsetError(f"Skillset {name} has no version {version!r} in the catalog")
        bundle = versions[version]
        owner = f"{name}@{version}"
        for member in bundle["skills"]:
            add(member["name"], name, member.get("version"), owner)
        for included in bundle["includes"]:
            versions_of = library.get(included)
            if not versions_of:
                raise SkillsetError(f"Skillset {included} (included by {owner}) is not in the catalog")
            if len(versions_of) != 1:
                raise SkillsetError(f"{owner} includes {included}, which has several versions; an include must resolve to exactly one")
            visit(included, next(iter(versions_of)), (*trail, name))

    for skill in direct:
        add(skill, "direct", None, "the selection")
    for entry in chosen:
        if not isinstance(entry, Mapping):
            raise SkillsetError("a selected Skillset is {name, version}")
        _check_keys(entry, {"name", "version"}, "selected skillset")
        visit(str(entry.get("name", "")), entry.get("version"), ())
    return [{"skill": skill, "version": available_skills[skill], "via": sorted(sources)} for skill, sources in sorted(via.items())]


def activation(effective: Iterable[Mapping[str, Any]], enabled: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Per-coworker state for every selected Skill: enabled only when named in ``enabled``, otherwise off.

    Enabling a Skill that was not selected is refused; selecting a bundle never enables any member.
    """
    rows = [dict(item) for item in effective]
    names = {row["skill"] for row in rows}
    wanted = set(enabled)
    unknown = sorted(wanted - names)
    if unknown:
        raise SkillsetError("cannot enable Skills that are not selected: " + ", ".join(unknown))
    return [{**row, "enabled": row["skill"] in wanted} for row in rows]


def exposed_tools(states: Iterable[Mapping[str, Any]], skill_tools: Mapping[str, Iterable[str]]) -> list[str]:
    """Tools reachable through enabled Skills only; a disabled or unselected member contributes nothing."""
    tools: set[str] = set()
    for row in states:
        if row.get("enabled"):
            tools.update(skill_tools.get(row["skill"], ()))
    return sorted(tools)


def compile_fields(states: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """The Agent ``spec.skills`` and ``spec.disabledSkills`` the existing compiler understands (selected, minus enabled)."""
    rows = list(states)
    return {"skills": [row["skill"] for row in rows], "disabledSkills": [row["skill"] for row in rows if not row.get("enabled")]}
