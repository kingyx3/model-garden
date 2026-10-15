"""Implementation-status consistency between the machine-readable registry and the technical docs.

The registry (``contracts/v1/integrations.json``) and the versioned tests/releases are the single
authority for what is built; prose must not contradict them. This module owns no I/O beyond reading
the repository and invents no status: it only reports claims that disagree with the registry.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ("README.md", "docs/*.md")
# Statuses that mean credential-free build evidence only; nothing here may be called live from unit tests.
TIER_ZERO_PROVIDERS = {"built-in-crm": "crm", "text-knowledge": "kb"}
NOT_BUILT = re.compile(r"\b(unimplemented|not (yet )?(implemented|built|shipped)|proposed|planned|NEXT)\b", re.IGNORECASE)
LIVE_CLAIM = re.compile(r"\b(production[- ]live|live tenant[- ]proven|is live|are live|now live)\b", re.IGNORECASE)
RECEPTIONIST_GAP = re.compile(r"receptionist\s+(does not|doesn't|has not)\s+(yet\s+)?(declare|select|use)", re.IGNORECASE)
NEGATED = re.compile(r"\b(not|never|no|until|nor|neither|without|unless|isn't|aren't)\b", re.IGNORECASE)


def _load_registry(root: pathlib.Path) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("model_garden_status_integrations", root / "platform/core/integrations.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.load_registry(root / "contracts/v1/integrations.json", root=root,
                                schema_path=root / "contracts/v1/integrations.schema.json",
                                catalog_path=root / "contracts/v1/capabilities.json")


def _doc_lines(root: pathlib.Path) -> list[tuple[str, int, str]]:
    lines: list[tuple[str, int, str]] = []
    for pattern in DOCS:
        for path in sorted(root.glob(pattern)):
            for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                lines.append((path.relative_to(root).as_posix(), number, text))
    return lines


def _mentions(text: str, provider: dict[str, Any]) -> bool:
    return f"`{provider['id']}`" in text or provider["name"] in text


def _reference_receptionist_families(root: pathlib.Path) -> set[str]:
    """Capability families the shipped reference receptionist's Skills require, read from the workspace files."""
    workspace = root / "examples" / "workspace"
    agents = [
        document for path in sorted((workspace / "agents").rglob("agent.yaml"))
        if isinstance(document := yaml.safe_load(path.read_text(encoding="utf-8")), dict)
        and document.get("metadata", {}).get("name") == "receptionist"
    ]
    if not agents:
        return set()
    selected = set(agents[0].get("spec", {}).get("skills", []))
    families: set[str] = set()
    for path in sorted((workspace / "skills").rglob("skill.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("metadata", {}).get("name") in selected:
            families.update(document.get("spec", {}).get("requiredCapabilities", []))
    return families


def check(root: pathlib.Path = ROOT) -> list[str]:
    """Return human-readable inconsistencies (empty when registry and docs agree)."""
    errors: list[str] = []
    registry = _load_registry(root)
    providers = {entry["id"]: entry for entry in registry["providers"]}

    for provider_id, family in TIER_ZERO_PROVIDERS.items():
        entry = providers.get(provider_id)
        if entry is None:
            errors.append(f"registry: Tier 0 provider {provider_id!r} is missing")
        elif entry["status"] != "built-credential-free" or entry["tier"] != 0 or entry["family"] != family or entry.get("proof"):
            errors.append(
                f"registry: {provider_id} must stay built-credential-free at tier 0 for {family} with no live proof "
                "until a real deployment is evidenced"
            )

    lines = _doc_lines(root)
    declared = _reference_receptionist_families(root)
    if {"kb", "crm"} <= declared:
        for where, number, text in lines:
            if RECEPTIONIST_GAP.search(text):
                errors.append(
                    f"{where}:{number}: says the reference receptionist does not declare capabilities, but its Skills "
                    f"require {', '.join(sorted(declared))}"
                )
    built = [p for p in providers.values() if p["status"] in ("built-credential-free", "live")]
    unproven = [p for p in providers.values() if p["status"] != "live"]
    for where, number, text in lines:
        # A line is judged as a whole sentence-group: a negation anywhere in it keeps a claim honest.
        for provider in built:
            if _mentions(text, provider) and NOT_BUILT.search(text) and not _lists_other_unbuilt(text, provider, providers):
                errors.append(
                    f"{where}:{number}: calls built provider {provider['id']!r} ({provider['status']}) "
                    f"unimplemented or planned"
                )
        for provider in unproven:
            if _mentions(text, provider) and LIVE_CLAIM.search(text) and not NEGATED.search(text):
                errors.append(
                    f"{where}:{number}: calls provider {provider['id']!r} live but the registry status is "
                    f"{provider['status']!r} with no retained proof"
                )
    return errors


def _lists_other_unbuilt(text: str, provider: dict[str, Any], providers: dict[str, Any]) -> bool:
    """True when the "not built" wording is about a different (planned) provider named on the same line."""
    others = [p for p in providers.values() if p["id"] != provider["id"] and p["status"] == "planned" and _mentions(text, p)]
    return bool(others)
