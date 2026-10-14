"""Canonical registry: validated integration inventory and non-secret connection plans."""
from __future__ import annotations
import json
import pathlib
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "contracts/v1/integrations.json"
SCHEMA = ROOT / "contracts/v1/integrations.schema.json"
CATALOG = ROOT / "contracts/v1/capabilities.json"
BUILT = {"live", "built-credential-free"}
METHOD_TIERS = {"built-in": 0, "file": 0, "native": 1, "governed-mcp": 2, "long-tail": 3}


def load_registry(path: pathlib.Path = REGISTRY, *, root: pathlib.Path = ROOT,
                  schema_path: pathlib.Path = SCHEMA, catalog_path: pathlib.Path = CATALOG):
    data = json.loads(path.read_text())
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(data)
    families = json.loads(catalog_path.read_text())["families"]
    seen = set()
    for entry in data["providers"]:
        name, family = entry["id"], entry["family"]
        if name in seen or family not in families:
            raise ValueError(f"duplicate provider or unknown family: {name}")
        seen.add(name)
        if entry["tier"] != METHOD_TIERS[entry["method"]]:
            raise ValueError(f"provider {name}: tier inconsistent with method")
        if any(tool not in families[family]["tools"] for tool in entry["tools"]):
            raise ValueError(f"provider {name}: unknown portable Tool")
        if any(tool not in entry["tools"] for tool in entry.get("scopeByTool", {})):
            raise ValueError(f"provider {name}: scope declared for unlisted Tool")
        if entry["status"] in BUILT and not (entry.get("adapter") and
                                               (root / entry["adapter"]).is_file()):
            raise ValueError(f"provider {name}: shipped status requires existing adapter")
        if entry["status"] == "live" and not entry.get("proof"):
            raise ValueError(f"provider {name}: live status requires retained proof")
    return data


def options(family: str, registry: dict | None = None):
    if family not in json.loads(CATALOG.read_text())["families"]:
        raise ValueError(f"unknown capability family {family}")
    return sorted((p for p in (registry or load_registry())["providers"] if p["family"] == family),
                  key=lambda p: (p["tier"], p["name"]))


def plan(family: str, provider_id: str, selected_tools: list[str]):
    match = [p for p in options(family) if p["id"] == provider_id]
    if not match:
        raise ValueError(f"unknown provider {provider_id!r} for {family}")
    provider = match[0]
    if not selected_tools or any(t not in provider["tools"] for t in selected_tools):
        raise ValueError("select only the portable Tools this provider supports")
    result = {"family": family, "provider": provider_id, "status": provider["status"],
              "tier": provider["tier"], "method": provider["method"],
              "tools": sorted(set(selected_tools)), "readyToConnect": False,
              "issue": provider["issue"]}
    if provider["status"] not in BUILT:
        result["blocker"] = "Connector not implemented; do not request credentials"
        return result
    scope_map = provider.get("scopeByTool", {})
    if any(t not in scope_map for t in selected_tools):
        raise ValueError("reviewed permission mapping absent for selected Tools")
    result["scopes"] = sorted({scope for tool in selected_tools for scope in scope_map[tool]})
    result["blocker"] = ("Client-owner consent, secure credential lifecycle and live read-only "
                         "test are required before changing the binding")
    return result


def render_directory(registry: dict | None = None) -> str:
    entries = (registry or load_registry())["providers"]
    lines = ["| Capability | Provider | Method | Tier | Status | Portable Tools | Issue |",
             "| --- | --- | --- | ---: | --- | --- | --- |"]
    for p in sorted(entries, key=lambda x: (x["family"], x["tier"], x["name"])):
        issue = p["issue"]
        lines.append(f"| {p['family']} | {p['name']} | {p['method']} | {p['tier']} | "
                     f"{p['status']} | {', '.join(p['tools'])} | "
                     f"[#{issue}](https://github.com/kingyx3/model-garden/issues/{issue}) |")
    return "\n".join(lines) + "\n"
