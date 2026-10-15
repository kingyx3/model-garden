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
import importlib.util
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
REFERENCE_FIELDS = {"skills": "Skill", "tools": "Tool", "knowledge": "KnowledgeSource", "policies": "Policy"}


def _load_capabilities():
    path = ROOT / "platform" / "core" / "capabilities.py"
    module_spec = importlib.util.spec_from_file_location("model_garden_capabilities", path)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


CAPABILITIES = _load_capabilities()


def _load_integrations():
    module_spec = importlib.util.spec_from_file_location(
        "model_garden_integrations", ROOT / "platform" / "core" / "integrations.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


INTEGRATIONS = _load_integrations()


def _load_policy():
    module_spec = importlib.util.spec_from_file_location(
        "model_garden_policy", ROOT / "platform" / "core" / "policy.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


POLICY = _load_policy()


def _load_knowledge():
    module_spec = importlib.util.spec_from_file_location(
        "model_garden_knowledge", ROOT / "platform" / "core" / "knowledge.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


KNOWLEDGE = _load_knowledge()


def load_validators() -> dict[str, Draft202012Validator]:
    validators: dict[str, Draft202012Validator] = {}
    for kind, filename in SCHEMAS.items():
        schema = json.loads((SCHEMA_DIR / filename).read_text())
        Draft202012Validator.check_schema(schema)
        validators[kind] = Draft202012Validator(schema)
    return validators


def _workspace_client(workspace: pathlib.Path) -> str | None:
    path = workspace / "modelgarden.yaml"
    if not path.is_file():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Workspace compilation failed: cannot parse {path}") from exc
    config = document.get("workspace") if isinstance(document, dict) else None
    client = config.get("client") if isinstance(config, dict) else None
    if not isinstance(client, str) or not client.strip():
        raise ValueError("Workspace compilation failed: modelgarden.yaml workspace.client must be a non-empty string")
    return client.strip()


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
            if kind == "Tool":
                contract_errors = CAPABILITIES.tool_contract_errors(document)
                errors.extend(f"{path}: {error}" for error in contract_errors)
                if contract_errors:
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


def materialize_knowledge_file(workspace, name, resource, source_paths, source_roots):
    """Bounded, root-confined content of a Tier 0 `text-file` KnowledgeSource, parsed as an upload test."""
    spec = resource.get("spec", {})
    if spec.get("type") != "text-file":
        return None, []
    key = ("KnowledgeSource", name)
    source_path = source_paths[key]
    source_root = source_roots[key].resolve()
    file_path = (source_path.parent / spec["file"]).resolve()
    try:
        text = KNOWLEDGE.read_text_file(file_path, root=source_root)
        report = KNOWLEDGE.check_source(text, filename=spec["file"])
    except KNOWLEDGE.KnowledgeError as exc:
        return None, [f"KnowledgeSource {name!r}: {exc}"]
    try:
        label = str(file_path.relative_to(workspace.resolve()))
    except ValueError:
        label = f"library:{file_path.relative_to(source_root).as_posix()}"
    return {"source": label, "content": text, "sha256": report["sha256"], "sectionCount": report["sectionCount"]}, []


def resolve_agent(workspace, agent, resources, source_paths, source_roots, environment_binding=None):
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
    # Disabled Skills remain business-authored but are not executable or
    # materialized into Hermes. Removing one from disabledSkills is an explicit
    # activation that must pass the ordinary capability and Tool checks.
    disabled = set(spec.get("disabledSkills", []))
    unknown_disabled = sorted(disabled - set(spec.get("skills", [])))
    if unknown_disabled:
        errors.append(f"Agent {name!r} disables Skills it did not select: {', '.join(unknown_disabled)}")
    listed_skills = resolved["skills"]
    resolved["skills"] = [
        skill for skill in listed_skills if skill["metadata"]["name"] not in disabled
    ]
    # Tools exclusively associated with a disabled Skill must not remain
    # callable merely because the Agent's Tool list still names them.
    disabled_tools = {
        tool
        for skill in listed_skills if skill["metadata"]["name"] in disabled
        for tool in skill.get("spec", {}).get("allowedTools", [])
    }
    enabled_tools = {
        tool for skill in resolved["skills"]
        for tool in skill.get("spec", {}).get("allowedTools", [])
    }
    suppressed_tools = disabled_tools - enabled_tools
    if suppressed_tools:
        resolved["tools"] = [
            tool for tool in resolved["tools"]
            if tool["metadata"]["name"] not in suppressed_tools
        ]
    agent_instructions, instruction_errors = materialize_instruction_file(
        workspace, "Agent", name, agent, source_paths, source_roots
    )
    errors.extend(instruction_errors)
    knowledge_content: dict[str, dict[str, Any]] = {}
    for knowledge in resolved["knowledge"]:
        knowledge_name = knowledge["metadata"]["name"]
        materialized_knowledge, knowledge_errors = materialize_knowledge_file(
            workspace, knowledge_name, knowledge, source_paths, source_roots
        )
        errors.extend(knowledge_errors)
        if materialized_knowledge is not None:
            knowledge_content[knowledge_name] = materialized_knowledge
    selected_tools = {tool["metadata"]["name"] for tool in resolved["tools"]}
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
    errors.extend(f"Agent {name!r}: {error}" for error in POLICY.relaxation_errors(resolved["policies"], resolved["tools"]))
    declarations = CAPABILITIES.collect_declarations(agent, resolved["skills"])
    errors.extend(CAPABILITIES.declaration_errors(declarations, f"Agent {name!r} or one of its Skills"))
    capabilities: dict[str, Any] | None = None
    if not errors and (declarations["required"] or declarations["optional"] or environment_binding is not None):
        capabilities = dict(declarations)
        if environment_binding is not None:
            try:
                capabilities.update(
                    CAPABILITIES.resolve(
                        declarations,
                        environment_binding,
                        agent_name=name,
                        required_by=CAPABILITIES.skills_requiring(resolved["skills"]),
                    )
                )
                capabilities["skills"] = CAPABILITIES.skill_readiness(
                    agent, listed_skills, environment_binding, disabled_skills=disabled
                )
            except ValueError as exc:
                errors.append(str(exc))
    if errors:
        raise ValueError("Workspace compilation failed:\n - " + "\n - ".join(errors))
    agent_path = source_paths[("Agent", name)]
    assert agent_instructions is not None
    source: dict[str, Any] = {
        "apiVersion": agent["apiVersion"],
        "kind": agent["kind"],
        "name": name,
        "path": str(agent_path.relative_to(workspace)),
    }
    client = _workspace_client(workspace)
    if client is not None:
        source["client"] = client
    compiled = {
        "schemaVersion": 1,
        "source": source,
        "agent": agent,
        "instructions": agent_instructions,
        "modelProfile": model_profile,
        "skills": resolved["skills"],
        "skillInstructions": skill_instructions,
        "tools": resolved["tools"],
        "knowledge": resolved["knowledge"],
    }
    if resolved["policies"]:
        compiled["policies"] = resolved["policies"]
    if knowledge_content:
        compiled["knowledgeContent"] = knowledge_content
    if capabilities is not None:
        compiled["capabilities"] = capabilities
    if disabled:
        # Preserve only capability declarations for disabled Skills. Their
        # instructions and Tools are never exported as executable resources.
        compiled["disabledSkillDeclarations"] = [
            {
                "metadata": {"name": skill["metadata"]["name"]},
                "spec": {
                    "requiredCapabilities": list(skill.get("spec", {}).get("requiredCapabilities", [])),
                    "optionalCapabilities": list(skill.get("spec", {}).get("optionalCapabilities", [])),
                },
            }
            for skill in listed_skills if skill["metadata"]["name"] in disabled
        ]
    return compiled


def compile_workspace(
    workspace: pathlib.Path,
    agent_name: str | None = None,
    library_roots: tuple[pathlib.Path, ...] = (),
    environment_binding: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compile Agents; with an EnvironmentBinding, also resolve declared capabilities.

    A required capability with no provider in that environment fails closed. An
    unbound optional capability compiles and is reported as degraded with its
    fallback tier.
    """
    resources, source_paths, source_roots = load_workspace(workspace, library_roots)
    return [
        resolve_agent(workspace, agent, resources, source_paths, source_roots, environment_binding)
        for agent in _selected_agents(resources, agent_name)
    ]


def _selected_agents(resources, agent_name: str | None) -> list[dict[str, Any]]:
    agents = resources["Agent"]
    if agent_name is None:
        return [agents[name] for name in sorted(agents)]
    agent = agents.get(agent_name)
    if agent is None:
        raise ValueError(f"Workspace compilation failed: Agent {agent_name!r} not found")
    return [agent]


def workspace_readiness(
    workspace: pathlib.Path,
    agent_name: str | None,
    library_roots: tuple[pathlib.Path, ...],
    environment_binding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per-Skill capability readiness for each selected Agent, without failing closed."""
    resources, _, _ = load_workspace(workspace, library_roots)
    report = []
    for agent in _selected_agents(resources, agent_name):
        configured = agent["spec"].get("skills", [])
        disabled = set(agent["spec"].get("disabledSkills", []))
        unknown_disabled = sorted(disabled - set(configured))
        if unknown_disabled:
            raise ValueError(
                f"Agent {agent['metadata']['name']!r} disables Skills it did not select: {', '.join(unknown_disabled)}"
            )
        absent = [ref for ref in configured if ref not in resources["Skill"]]
        if absent:
            raise ValueError(f"Agent {agent['metadata']['name']!r} refers to missing Skills: {', '.join(absent)}")
        skills = [resources["Skill"][ref] for ref in configured]
        report.append({
            "agent": agent["metadata"]["name"],
            "environment": CAPABILITIES.binding_environment(environment_binding),
            "skills": CAPABILITIES.skill_readiness(
                agent, skills, environment_binding, disabled_skills=disabled
            ),
        })
    return report


def workspace_approvals(
    workspace: pathlib.Path,
    agent_name: str | None,
    library_roots: tuple[pathlib.Path, ...],
) -> list[dict[str, Any]]:
    """Per-Skill view of which Tool actions run automatically, need approval or are denied."""
    resources, _, _ = load_workspace(workspace, library_roots)
    report = []
    for agent in _selected_agents(resources, agent_name):
        spec = agent["spec"]
        name = agent["metadata"]["name"]
        missing = [
            f"{kind} {ref!r}"
            for field, kind in (("skills", "Skill"), ("tools", "Tool"), ("policies", "Policy"))
            for ref in spec.get(field, [])
            if ref not in resources[kind]
        ]
        if missing:
            raise ValueError(f"Agent {name!r} references missing {', '.join(missing)}")
        policies = [resources["Policy"][ref] for ref in spec.get("policies", [])]
        tools = {ref: resources["Tool"][ref] for ref in spec.get("tools", [])}
        classification = spec.get("security", {}).get("dataClassification")
        agent_requires = spec.get("approvals", {}).get("externalActions") == "required"
        disabled = set(spec.get("disabledSkills", []))

        def row(tool_name: str) -> dict[str, Any]:
            tool = tools[tool_name]["spec"]
            return {
                "tool": tool_name,
                "risk": tool["risk"],
                **POLICY.preview(
                    policies,
                    tool_name=tool_name,
                    risk=tool["risk"],
                    classification=classification,
                    agent_requires_approval=agent_requires,
                    tool_requires_approval=bool(tool.get("approvalRequired", False)),
                ),
            }

        skills = []
        covered: set[str] = set()
        for ref in spec.get("skills", []):
            if ref in disabled:
                continue
            allowed = [tool for tool in resources["Skill"][ref].get("spec", {}).get("allowedTools", []) if tool in tools]
            covered.update(allowed)
            skills.append({"skill": ref, "actions": [row(tool) for tool in allowed]})
        skills.append({"skill": None, "actions": [row(tool) for tool in sorted(set(tools) - covered)]})
        report.append({
            "agent": name,
            "policyVersion": POLICY.policy_version(policies),
            "skills": [entry for entry in skills if entry["actions"]],
        })
    return report


def format_approvals(report: list[dict[str, Any]]) -> str:
    lines = []
    for agent in report:
        lines.append(f"Agent {agent['agent']} (approval policy {agent['policyVersion']})")
        for entry in agent["skills"]:
            lines.append(f"  Skill {entry['skill']}" if entry["skill"] else "  Agent-level Tools")
            width = max(len(action["tool"]) for action in entry["actions"])
            for action in entry["actions"]:
                lines.append(
                    f"    {action['tool']:<{width}}  {action['risk']:<22}"
                    f" members: {action['member']:<9} public callers: {action['public']}"
                )
        lines.append("  Approvals are given by a mapped approver from the client principal directory; public callers never approve.")
    return "\n".join(lines) + "\n"


def _dependency_text(dependency: dict[str, Any]) -> str:
    label = dependency["capability"] + ("" if dependency["required"] else " (optional)")
    if dependency["status"] == "bound":
        return f"{label}: {dependency['provider']} tier {dependency['tier']}"
    if dependency["status"] == "missing":
        return f"{label}: MISSING"
    return f"{label}: degraded -> fallback tier {dependency['fallbackTier']}"


def format_readiness(report: list[dict[str, Any]]) -> str:
    lines = []
    for agent in report:
        where = agent["environment"] or "target environment"
        lines.append(f"Agent {agent['agent']} in {where}")
        width = max([len(entry["name"]) for entry in agent["skills"]] + [5])
        for entry in agent["skills"]:
            state = "DISABLED" if not entry.get("enabled", True) else ("ready" if entry["ready"] else "BLOCKED")
            needs = "; ".join(_dependency_text(item) for item in entry["dependencies"]) or "-"
            lines.append(f"  {entry['kind']:<5} {entry['name']:<{width}}  {state:<7}  {needs}")
            missing_families = sorted({item["family"] for item in entry["dependencies"] if item["status"] == "missing"})
            for family in missing_families:
                choices = INTEGRATIONS.options(family)
                built = [choice["id"] for choice in choices if choice["status"] in INTEGRATIONS.BUILT]
                hint = (", ".join(built) if built else "no usable connector shipped yet")
                lines.append(f"    Next: python3 scripts/client-operator.py connect {family}  ({hint})")
    return "\n".join(lines) + "\n"


def load_environment_binding(path: pathlib.Path) -> dict[str, Any]:
    """Load a rendered EnvironmentBinding JSON or a client environments/<env>.yaml file."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Workspace compilation failed: cannot parse environment binding {path}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"Workspace compilation failed: environment binding {path} must be a mapping")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", nargs="?", default="examples/workspace")
    parser.add_argument("--agent", help="Compile only the named Agent")
    parser.add_argument("--library", action="append", default=[], help="curated resource root; may be repeated and is overridden by client-local resources")
    parser.add_argument("--output", help="Write JSON to this path instead of stdout")
    parser.add_argument(
        "--environment-binding",
        help="EnvironmentBinding JSON or environment YAML; resolves declared capabilities and fails closed on unbound required ones",
    )
    parser.add_argument(
        "--readiness",
        action="store_true",
        help="print per-Skill capability readiness for --environment-binding and exit non-zero if any Skill is blocked",
    )
    parser.add_argument(
        "--approvals",
        action="store_true",
        help="print which Tool actions run automatically, need approval or are denied, per Skill, and exit",
    )
    args = parser.parse_args()
    workspace = pathlib.Path(args.workspace)
    libraries = tuple(pathlib.Path(path) for path in args.library)
    if args.approvals:
        try:
            report = workspace_approvals(workspace, args.agent, libraries)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        sys.stdout.write(format_approvals(report))
        return 0
    if args.readiness:
        if not args.environment_binding:
            parser.error("--readiness requires --environment-binding")
        try:
            binding = load_environment_binding(pathlib.Path(args.environment_binding))
            report = workspace_readiness(workspace, args.agent, libraries, binding)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        sys.stdout.write(format_readiness(report))
        return 0 if all(entry["ready"] or not entry.get("enabled", True)
                        for agent in report for entry in agent["skills"]) else 1
    try:
        binding = load_environment_binding(pathlib.Path(args.environment_binding)) if args.environment_binding else None
        compiled = compile_workspace(workspace, args.agent, libraries, binding)
    except (OSError, ValueError) as exc:
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
