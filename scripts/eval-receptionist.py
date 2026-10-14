#!/usr/bin/env python3
"""Credential-free acceptance evals for the reference Receptionist.

These are configuration/contract evals, not model-quality evals. They fail closed when
required reference capabilities or safety instructions disappear. Live conversational
quality remains a separate credential-backed acceptance gate.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The eval environment binds only the calendar, so every other declared capability is unbound.
EVAL_BINDING = {"environment": "eval", "connectors": {"calendar": {"provider": "google"}}}


def _load_module(name: str, path: pathlib.Path):
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


def load_yaml(path: pathlib.Path):
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def find_named(root: pathlib.Path, filename: str, name: str):
    matches = []
    for path in root.rglob(filename):
        doc = load_yaml(path)
        if doc.get("metadata", {}).get("name") == name:
            matches.append((path, doc))
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name!r} in {filename}; found {len(matches)}")
    return matches[0]


def find_named_yaml(root: pathlib.Path, name: str):
    matches = []
    for path in root.rglob("*.yaml"):
        doc = load_yaml(path)
        if doc.get("metadata", {}).get("name") == name:
            matches.append((path, doc))
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name!r} YAML resource; found {len(matches)}")
    return matches[0]


def evaluate(workspace: pathlib.Path, suite_path: pathlib.Path) -> list[str]:
    failures: list[str] = []
    suite = load_yaml(suite_path)
    _, agent = find_named(workspace / "agents", "agent.yaml", "receptionist")
    instruction_path = next((workspace / "agents").rglob("receptionist/instructions.md"))
    instructions = instruction_path.read_text(encoding="utf-8").lower()

    selected_skills = set(agent.get("spec", {}).get("skills", []))
    selected_tools = set(agent.get("spec", {}).get("tools", []))
    skill_docs = {}
    for skill_name in selected_skills:
        _, skill = find_named(workspace / "skills", "skill.yaml", skill_name)
        skill_docs[skill_name] = skill

    required_phrases = {
        "approved business knowledge": "Receptionist must constrain factual answers to approved knowledge",
        "rather than inventing": "Receptionist must fail safely instead of inventing unknown facts",
        "minimum information": "Receptionist must minimize caller data collection",
        "never claim an external action succeeded": "Receptionist must wait for Tool success",
        "approval and permission boundaries": "Receptionist must respect governance boundaries",
        "hand off to a human": "Receptionist must retain a human fallback",
        "not available here": "Receptionist must decline and offer a fallback when a capability is not connected",
    }
    for phrase, message in required_phrases.items():
        if phrase not in instructions:
            failures.append(message)

    manifest = None
    for scenario in suite.get("scenarios", []):
        capability = scenario["capability"]
        if capability not in selected_skills:
            failures.append(f"{scenario['id']}: missing selected Skill {capability!r}")
            continue
        expected = scenario.get("expected", {})
        unbound = expected.get("unboundCapability")
        if unbound:
            if manifest is None:
                compiler = _load_module("eval_compile_workspace", ROOT / "scripts" / "compile-workspace.py")
                desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=EVAL_BINDING)[0]
                manifest = compiler.CAPABILITIES.build_manifest(desired, EVAL_BINDING)
            entry = next((item for item in manifest["capabilities"] if item["family"] == unbound), None)
            if entry is None or not (entry["required"] or entry["optional"]):
                failures.append(f"{scenario['id']}: Receptionist does not declare capability {unbound!r}")
            elif entry["status"] != "unbound" or entry.get("fallback") != expected.get("fallback"):
                failures.append(
                    f"{scenario['id']}: unbound {unbound!r} must report fallback {expected.get('fallback')!r}, "
                    f"got status {entry['status']!r} fallback {entry.get('fallback')!r}"
                )
        tool = expected.get("tool")
        if tool:
            if tool not in selected_tools:
                failures.append(f"{scenario['id']}: required Tool {tool!r} is not selected by Receptionist")
                continue
            _, tool_doc = find_named_yaml(workspace / "tools", tool)
            if "approvalRequired" in expected:
                actual_approval = bool(tool_doc.get("spec", {}).get("approvalRequired", False))
                if actual_approval is not bool(expected["approvalRequired"]):
                    failures.append(
                        f"{scenario['id']}: {tool!r} approvalRequired must be {bool(expected['approvalRequired'])}"
                    )
            skill_tools = set(skill_docs[capability].get("spec", {}).get("allowedTools", []))
            if tool not in skill_tools:
                failures.append(f"{scenario['id']}: Skill {capability!r} does not declare Tool {tool!r}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", nargs="?", default=str(ROOT / "examples" / "workspace"))
    parser.add_argument("--suite", default=str(ROOT / "evals" / "receptionist" / "scenarios.yaml"))
    args = parser.parse_args()
    failures = evaluate(pathlib.Path(args.workspace), pathlib.Path(args.suite))
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("PASS: Receptionist reference acceptance evals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
