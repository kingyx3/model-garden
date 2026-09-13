#!/usr/bin/env python3
"""Compile a validated Model Garden client workspace into deterministic runtime desired state.

The compiler intentionally stays small for the MVP: it validates versioned resources,
resolves an Agent's references by metadata.name, materializes its instruction file, and
emits secret-free JSON. Runtime-specific provisioning remains the responsibility of an
adapter (Hermes is the reference runtime).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts" / "v1"
SCHEMAS = {
    "Agent": "agent.schema.json",
    "Skill": "skill.schema.json",
    "ModelProfile": "model-profile.schema.json",
    "Tool": "tool.schema.json",
    "KnowledgeSource": "knowledge-source.schema.json",
    "Policy": "policy.schema.json",
}

REFERENCE_FIELDS = {
    "skills": "Skill",
    "tools": "Tool",
    "knowledge": "KnowledgeSource",
}


def load_validators() -> dict[str, Draft202012Validator]:
    validators: dict[str, Draft202012Validator] = {}
    for kind, filename in SCHEMAS.items():
        schema = json.loads((SCHEMA_DIR / filename).read_text())
        Draft202012Validator.check_schema(schema)
        validators[kind] = Draft202012Validator(schema)
    return validators


def load_workspace(workspace: pathlib.Path) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[tuple[str, str], pathlib.Path]]:
    validators = load_validators()
    resources: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in SCHEMAS}
    source_paths: dict[tuple[str, str], pathlib.Path] = {}
    errors: list[str] = []

    for path in sorted(workspace.rglob("*.yaml")):
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict) or "kind" not in document:
            continue
        kind = document.get("kind")
        if kind not in validators:
            errors.append(f"{path}: unsupported kind {kind!r}")
            continue
        schema_errors = sorted(validators[kind].iter_errors(document), key=lambda e: list(e.path))
        for error in schema_errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            errors.append(f"{path}:{location}: {error.message}")
        if schema_errors:
            continue
        name = document["metadata"]["name"]
        if name in resources[kind]:
            errors.append(f"{path}: duplicate {kind} metadata.name {name!r}")
            continue
        resources[kind][name] = document
        source_paths[(kind, name)] = path

    if errors:
        raise ValueError("Workspace compilation failed:\n - " + "\n - ".join(errors))
    if not resources["Agent"]:
        raise ValueError(f"Workspace compilation failed: no Agent resources found under {workspace}")
    return resources, source_paths


def resolve_agent(
    workspace: pathlib.Path,
    agent: dict[str, Any],
    resources: dict[str, dict[str, dict[str, Any]]],
    source_paths: dict[tuple[str, str], pathlib.Path],
) -> dict[str, Any]:
    name = agent["metadata"]["name"]
    spec = agent["spec"]
    errors: list[str] = []

    model_name = spec["model"]["profile"]
    model_profile = resources["ModelProfile"].get(model_name)
    if model_profile is None:
        errors.append(f"Agent {name!r} references missing ModelProfile {model_name!r}")

    resolved: dict[str, list[dict[str, Any]]] = {}
    for field, kind in REFERENCE_FIELDS.items():
        resolved[field] = []
        for ref in spec.get(field, []):
            resource = resources[kind].get(ref)
            if resource is None:
                errors.append(f"Agent {name!r} references missing {kind} {ref!r}")
            else:
                resolved[field].append(resource)

    agent_path = source_paths[("Agent", name)]
    instruction_path = (agent_path.parent / spec["instructions"]["file"]).resolve()
    workspace_root = workspace.resolve()
    try:
        instruction_path.relative_to(workspace_root)
    except ValueError:
        errors.append(f"Agent {name!r} instruction path escapes workspace: {instruction_path}")
    if not instruction_path.is_file():
        errors.append(f"Agent {name!r} instruction file does not exist: {instruction_path}")

    if errors:
        raise ValueError("Workspace compilation failed:\n - " + "\n - ".join(errors))

    return {
        "schemaVersion": 1,
        "source": {
            "apiVersion": agent["apiVersion"],
            "kind": agent["kind"],
            "name": name,
            "path": str(agent_path.relative_to(workspace)),
        },
        "agent": agent,
        "instructions": {
            "source": str(instruction_path.relative_to(workspace_root)),
            "content": instruction_path.read_text(),
        },
        "modelProfile": model_profile,
        "skills": resolved["skills"],
        "tools": resolved["tools"],
        "knowledge": resolved["knowledge"],
    }


def compile_workspace(workspace: pathlib.Path, agent_name: str | None = None) -> list[dict[str, Any]]:
    resources, source_paths = load_workspace(workspace)
    agents = resources["Agent"]
    if agent_name is not None:
        agent = agents.get(agent_name)
        if agent is None:
            raise ValueError(f"Workspace compilation failed: Agent {agent_name!r} not found")
        selected = [agent]
    else:
        selected = [agents[name] for name in sorted(agents)]
    return [resolve_agent(workspace, agent, resources, source_paths) for agent in selected]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", nargs="?", default="examples/workspace")
    parser.add_argument("--agent", help="Compile only the named Agent")
    parser.add_argument("--output", help="Write JSON to this path instead of stdout")
    args = parser.parse_args()

    workspace = pathlib.Path(args.workspace)
    try:
        compiled = compile_workspace(workspace, args.agent)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload: Any = compiled[0] if args.agent else {"schemaVersion": 1, "agents": compiled}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
        print(f"Compiled {len(compiled)} Agent(s) from {workspace} to {output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
