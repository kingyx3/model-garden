#!/usr/bin/env python3
"""Compile a validated Model Garden client workspace into deterministic runtime desired state.

The compiler intentionally stays small for the MVP: it validates versioned resources,
resolves an Agent's references by metadata.name, materializes Agent and Skill instruction
files, enforces that Skills cannot smuggle in Tools the Agent did not select, and emits
secret-free JSON. Optional library roots let a separate client workspace consume pinned,
curated resources without copying them into the client repository. Runtime-specific
provisioning remains the responsibility of an adapter (Hermes is the reference runtime).
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
REFERENCE_FIELDS = {"skills": "Skill", "tools": "Tool", "knowledge": "KnowledgeSource"}


def load_validators() -> dict[str, Draft202012Validator]:
    validators: dict[str, Draft202012Validator] = {}
    for kind, filename in SCHEMAS.items():
        schema = json.loads((SCHEMA_DIR / filename).read_text())
        Draft202012Validator.check_schema(schema)
        validators[kind] = Draft202012Validator(schema)
    return validators


def load_workspace(workspace: pathlib.Path, library_roots: tuple[pathlib.Path, ...] = ()):
    validators = load_validators()
    resources: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in SCHEMAS}
    source_paths: dict[tuple[str, str], pathlib.Path] = {}
    source_roots: dict[tuple[str, str], pathlib.Path] = {}
    errors: list[str] = []
    roots = [(root, False) for root in library_roots] + [(workspace, True)]
    seen_library: set[tuple[str, str]] = set()

    for root, is_workspace in roots:
        if not root.is_dir():
            errors.append(f"resource root does not exist or is not a directory: {root}")
            continue
        seen_in_root: set[tuple[str, str]] = set()
        for path in sorted(root.rglob("*.yaml")):
            document = yaml.safe_load(path.read_text())
            if not isinstance(document, dict) or "kind" not in document:
                continue
            kind = document.get("kind")
            if kind not in validators:
                errors.append(f"{path}: unsupported kind {kind!r}")
                continue
            if not is_workspace and kind == "Agent":
                continue
            schema_errors = sorted(validators[kind].iter_errors(document), key=lambda e: list(e.path))
            for error in schema_errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                errors.append(f"{path}:{location}: {error.message}")
            if schema_errors:
                continue
            name = document["metadata"]["name"]
            key = (kind, name)
            if key in seen_in_root:
                errors.append(f"{path}: duplicate {kind} metadata.name {name!r}")
                continue
            seen_in_root.add(key)
            if not is_workspace and key in seen_library:
                errors.append(f"{path}: duplicate library {kind} metadata.name {name!r}")
                continue
            if not is_workspace:
                seen_library.add(key)
            resources[kind][name] = document
            source_paths[key] = path
            source_roots[key] = root

    if errors:
        raise ValueError("Workspace compilation failed:\n - " + "\n - ".join(errors))
    if not resources["Agent"]:
        raise ValueError(f"Workspace compilation failed: no Agent resources found under {workspace}")
    return resources, source_paths, source_roots


def materialize_instruction_file(workspace, resource_kind, resource_name, resource, source_paths, source_roots):
    instructions = resource.get("spec", {}).get("instructions")
    if not isinstance(instructions, dict) or not instructions.get("file"):
        return None, []
    key = (resource_kind, resource_name)
    source_path = source_paths[key]
    source_root = source_roots[key].resolve()
    instruction_path = (source_path.parent / instructions["file"]).resolve()
    errors: list[str] = []
    try:
        relative_instruction = instruction_path.relative_to(source_root)
    except ValueError:
        boundary = "workspace" if source_root == workspace.resolve() else "library root"
        errors.append(f"{resource_kind} {resource_name!r} instruction path escapes {boundary}: {instruction_path}")
        return None, errors
    if not instruction_path.is_file():
        errors.append(f"{resource_kind} {resource_name!r} instruction file does not exist: {instruction_path}")
        return None, errors
    try:
        source = str(instruction_path.relative_to(workspace.resolve()))
    except ValueError:
        source = f"library:{relative_instruction.as_posix()}"
    return {"source": source, "content": instruction_path.read_text()}, errors


def resolve_agent(workspace, agent, resources, source_paths, source_roots):
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
    agent_instructions, instruction_errors = materialize_instruction_file(
        workspace, "Agent", name, agent, source_paths, source_roots
    )
    errors.extend(instruction_errors)
    selected_tools = set(spec.get("tools", []))
    skill_instructions: dict[str, dict[str, str]] = {}
    for skill in resolved["skills"]:
        skill_name = skill["metadata"]["name"]
        materialized, skill_errors = materialize_instruction_file(
            workspace, "Skill", skill_name, skill, source_paths, source_roots
        )
        errors.extend(skill_errors)
        if materialized is not None:
            skill_instructions[skill_name] = materialized
        for tool_name in skill.get("spec", {}).get("allowedTools", []):
            if tool_name not in selected_tools:
                errors.append(f"Skill {skill_name!r} requires Tool {tool_name!r}, but Agent {name!r} did not select it")
    if errors:
        raise ValueError("Workspace compilation failed:\n - " + "\n - ".join(errors))
    agent_path = source_paths[("Agent", name)]
    assert agent_instructions is not None
    return {
        "schemaVersion": 1,
        "source": {"apiVersion": agent["apiVersion"], "kind": agent["kind"], "name": name, "path": str(agent_path.relative_to(workspace))},
        "agent": agent,
        "instructions": agent_instructions,
        "modelProfile": model_profile,
        "skills": resolved["skills"],
        "skillInstructions": skill_instructions,
        "tools": resolved["tools"],
        "knowledge": resolved["knowledge"],
    }


def compile_workspace(workspace: pathlib.Path, agent_name: str | None = None, library_roots: tuple[pathlib.Path, ...] = ()) -> list[dict[str, Any]]:
    resources, source_paths, source_roots = load_workspace(workspace, library_roots)
    agents = resources["Agent"]
    if agent_name is not None:
        agent = agents.get(agent_name)
        if agent is None:
            raise ValueError(f"Workspace compilation failed: Agent {agent_name!r} not found")
        selected = [agent]
    else:
        selected = [agents[name] for name in sorted(agents)]
    return [resolve_agent(workspace, agent, resources, source_paths, source_roots) for agent in selected]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", nargs="?", default="examples/workspace")
    parser.add_argument("--agent", help="Compile only the named Agent")
    parser.add_argument("--library", action="append", default=[], help="curated resource root; may be repeated and is overridden by client-local resources")
    parser.add_argument("--output", help="Write JSON to this path instead of stdout")
    args = parser.parse_args()
    workspace = pathlib.Path(args.workspace)
    libraries = tuple(pathlib.Path(path) for path in args.library)
    try:
        compiled = compile_workspace(workspace, args.agent, libraries)
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
