#!/usr/bin/env python3
"""Credential-free acceptance evals for the reference Receptionist.

These are configuration/contract evals, not model-quality evals. They fail closed when
required reference capabilities or safety instructions disappear. Live conversational
quality remains a separate credential-backed acceptance gate.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The eval environment binds the calendar and the credential-free Tier 0 knowledge and contact
# log, so every other declared capability (tickets) is unbound and must degrade to a hand-off.
EVAL_BINDING = {
    "environment": "eval",
    "connectors": {
        "calendar": {"provider": "google"},
        "kb": {"provider": "text-knowledge", "tier": 0},
        "crm": {"provider": "built-in-crm", "tier": 0},
    },
}
# Business-rules content is only searchable once effective; evaluate as of a fixed date.
EVAL_DATE = datetime.date(2026, 6, 1)
RETRIEVAL_TOP = 3


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

    state: dict[str, object] = {}

    def compiled():
        """Compile once against the evaluation binding; a failure is reported, never raised."""
        if "error" in state:
            return None
        if "desired" not in state:
            compiler = _load_module("eval_compile_workspace", ROOT / "scripts" / "compile-workspace.py")
            try:
                desired = compiler.compile_workspace(workspace, "receptionist", environment_binding=EVAL_BINDING)[0]
            except ValueError as exc:
                state["error"] = str(exc)
                failures.append(f"Receptionist does not compile against the evaluation binding: {exc}")
                return None
            state.update(
                compiler=compiler,
                desired=desired,
                manifest=compiler.CAPABILITIES.build_manifest(desired, EVAL_BINDING),
            )
        return state

    for scenario in suite.get("scenarios", []):
        capability = scenario["capability"]
        if capability not in selected_skills:
            failures.append(f"{scenario['id']}: missing selected Skill {capability!r}")
            continue
        expected = scenario.get("expected", {})
        bound = expected.get("boundCapability")
        if bound:
            context = compiled()
            entry = None
            if context is not None:
                entry = next((item for item in context["manifest"]["capabilities"] if item["family"] == bound["family"]), None)
            if context is not None and (
                entry is None
                or entry.get("status") != "available"
                or entry.get("provider") != bound["provider"]
                or entry.get("tier") != bound["tier"]
            ):
                failures.append(
                    f"{scenario['id']}: capability {bound['family']!r} must be available from {bound['provider']!r} "
                    f"at tier {bound['tier']} in the evaluation binding"
                )
        retrieval = expected.get("retrieval")
        if retrieval:
            context = compiled()
            if context is not None:
                found = context["compiler"].KNOWLEDGE.search_selected(
                    context["desired"], scenario["input"], today=EVAL_DATE
                )
                cited = [document["citation"] for document in found["documents"][:RETRIEVAL_TOP]]
                if "citation" in retrieval and retrieval["citation"] not in cited:
                    failures.append(
                        f"{scenario['id']}: expected a search for {scenario['input']!r} to cite "
                        f"{retrieval['citation']!r} in its top {RETRIEVAL_TOP}, got {cited}"
                    )
                if "documents" in retrieval and len(found["documents"]) != retrieval["documents"]:
                    failures.append(
                        f"{scenario['id']}: expected {retrieval['documents']} section(s) for {scenario['input']!r}, "
                        f"got {len(found['documents'])}; an unknown question must fall back to a person"
                    )
        unbound = expected.get("unboundCapability")
        if unbound:
            context = compiled()
            manifest = context["manifest"] if context is not None else {"capabilities": []}
            entry = next((item for item in manifest["capabilities"] if item["family"] == unbound), None)
            if entry is None or not (entry["required"] or entry["optional"]):
                failures.append(f"{scenario['id']}: Receptionist does not declare capability {unbound!r}")
            elif entry["status"] != "unbound" or entry.get("fallback") != expected.get("fallback"):
                failures.append(
                    f"{scenario['id']}: unbound {unbound!r} must report fallback {expected.get('fallback')!r}, "
                    f"got status {entry['status']!r} fallback {entry.get('fallback')!r}"
                )
        handoff_tool = expected.get("handoffTool")
        if handoff_tool:
            provisioner = _load_module("eval_provision_hermes", ROOT / "scripts" / "provision-hermes-profile.py")
            status_tool = expected.get("statusTool", "")
            for name in (handoff_tool, status_tool):
                if name not in provisioner.PLATFORM_MCP_TOOLS:
                    failures.append(f"{scenario['id']}: platform Tool {name!r} is not exposed to the runtime")
                if name not in instructions:
                    failures.append(f"{scenario['id']}: Receptionist instructions must tell it to use {name!r}")
            if "not done until the person confirms" not in instructions:
                failures.append(f"{scenario['id']}: Receptionist must say a handed-off action is not done until a person confirms")
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
