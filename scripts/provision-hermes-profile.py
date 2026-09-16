#!/usr/bin/env python3
"""Materialize Model Garden runtime desired state into a disposable Hermes profile.

This adapter intentionally owns only runtime-specific file materialization. It never
writes credentials, starts Hermes, or grants Tools beyond the compiled desired state.
Selected supported Tools are exposed through the Model Garden governed MCP sidecar;
connector credentials remain outside this profile.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

import yaml

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
MANAGED_STATE = pathlib.Path(".modelgarden") / "desired-state.json"
MANAGED_SKILL_ROOT = pathlib.Path("skills") / "model-garden"
MCP_TOOL_NAMES = {
    "calendar.availability": "calendar_availability",
    "calendar.book": "calendar_book",
}


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Hermes provisioning failed: {label} must be an object")
    return value


def _safe_name(name: Any, label: str) -> str:
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise ValueError(
            f"Hermes provisioning failed: {label} {name!r} contains unsupported path characters"
        )
    return name


def _selected_mcp_tools(desired_state: dict[str, Any]) -> list[str]:
    tools = desired_state.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("Hermes provisioning failed: tools must be an array")
    selected: list[str] = []
    unsupported: list[str] = []
    for value in tools:
        tool = _require_mapping(value, "Tool")
        metadata = _require_mapping(tool.get("metadata"), "Tool metadata")
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Hermes provisioning failed: Tool metadata.name must be non-empty")
        runtime_name = MCP_TOOL_NAMES.get(name)
        if runtime_name is None:
            unsupported.append(name)
        else:
            selected.append(runtime_name)
    if unsupported:
        raise ValueError(
            "Hermes provisioning failed: no governed runtime implementation for selected Tool(s): "
            + ", ".join(sorted(unsupported))
        )
    return sorted(selected)


def _model_config(desired_state: dict[str, Any]) -> dict[str, Any]:
    profile = _require_mapping(desired_state.get("modelProfile"), "modelProfile")
    candidates = profile.get("spec", {}).get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Hermes provisioning failed: modelProfile has no candidates")
    candidate = _require_mapping(candidates[0], "first model candidate")
    provider = candidate.get("provider")
    model = candidate.get("model")
    if not isinstance(provider, str) or not provider:
        raise ValueError("Hermes provisioning failed: first model candidate has no provider")
    if not isinstance(model, str) or not model:
        raise ValueError("Hermes provisioning failed: first model candidate has no model")
    config: dict[str, Any] = {"model": {"provider": provider, "default": model}}
    tools = _selected_mcp_tools(desired_state)
    if tools:
        config["mcp_servers"] = {
            "model_garden": {
                "url": "http://governed-tools:9120/mcp",
                "enabled": True,
                "timeout": 30,
                "connect_timeout": 10,
                "supports_parallel_tool_calls": False,
                "tools": {
                    "include": tools,
                    "exclude": [],
                    "resources": False,
                    "prompts": False,
                },
            }
        }
    return config


def _skill_document(skill: dict[str, Any], instructions: str) -> str:
    metadata = _require_mapping(skill.get("metadata"), "Skill metadata")
    name = _safe_name(metadata.get("name"), "Skill name")
    description = metadata.get("description") or f"Model Garden Skill {name}"
    version = metadata.get("version") or "1.0.0"
    frontmatter = {
        "name": name,
        "description": str(description),
        "version": str(version),
        "metadata": {
            "modelgarden": {
                "managed": True,
                "allowedTools": skill.get("spec", {}).get("allowedTools", []),
            }
        },
    }
    return "---\n" + yaml.safe_dump(frontmatter, sort_keys=False).rstrip() + "\n---\n\n" + instructions.rstrip() + "\n"


def build_profile_files(desired_state: dict[str, Any]) -> dict[pathlib.Path, str]:
    if desired_state.get("schemaVersion") != 1:
        raise ValueError("Hermes provisioning failed: desired state schemaVersion must be 1")

    agent = _require_mapping(desired_state.get("agent"), "agent")
    metadata = _require_mapping(agent.get("metadata"), "agent.metadata")
    _safe_name(metadata.get("name"), "Agent name")

    instructions = _require_mapping(desired_state.get("instructions"), "instructions")
    soul = instructions.get("content")
    if not isinstance(soul, str) or not soul.strip():
        raise ValueError("Hermes provisioning failed: Agent instructions are empty")

    skill_instructions = _require_mapping(desired_state.get("skillInstructions", {}), "skillInstructions")
    skills = desired_state.get("skills", [])
    if not isinstance(skills, list):
        raise ValueError("Hermes provisioning failed: skills must be an array")

    files: dict[pathlib.Path, str] = {
        pathlib.Path("SOUL.md"): soul.rstrip() + "\n",
        pathlib.Path("config.yaml"): yaml.safe_dump(_model_config(desired_state), sort_keys=False),
        MANAGED_STATE: json.dumps(desired_state, indent=2, sort_keys=True) + "\n",
    }

    seen: set[str] = set()
    for skill_value in skills:
        skill = _require_mapping(skill_value, "Skill")
        skill_metadata = _require_mapping(skill.get("metadata"), "Skill metadata")
        name = _safe_name(skill_metadata.get("name"), "Skill name")
        if name in seen:
            raise ValueError(f"Hermes provisioning failed: duplicate Skill {name!r}")
        seen.add(name)
        materialized = skill_instructions.get(name)
        if not isinstance(materialized, dict) or not isinstance(materialized.get("content"), str):
            raise ValueError(f"Hermes provisioning failed: Skill {name!r} has no materialized instructions")
        files[MANAGED_SKILL_ROOT / name / "SKILL.md"] = _skill_document(
            skill, materialized["content"]
        )

    return files


def _stale_managed_skill_files(
    profile_dir: pathlib.Path,
    files: dict[pathlib.Path, str],
) -> list[pathlib.Path]:
    managed_root = profile_dir / MANAGED_SKILL_ROOT
    if not managed_root.exists():
        return []
    if managed_root.is_symlink():
        raise ValueError("Hermes provisioning failed: managed Skill root may not be a symlink")
    desired = {path for path in files if path.parts[:2] == MANAGED_SKILL_ROOT.parts}
    stale: list[pathlib.Path] = []
    for path in managed_root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Hermes provisioning failed: managed Skill path may not be a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(profile_dir)
            if relative not in desired:
                stale.append(relative)
    return sorted(stale, key=lambda path: path.as_posix())


def _remove_empty_managed_skill_dirs(profile_dir: pathlib.Path) -> None:
    managed_root = profile_dir / MANAGED_SKILL_ROOT
    if not managed_root.is_dir():
        return
    directories = [path for path in managed_root.rglob("*") if path.is_dir()]
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
    try:
        managed_root.rmdir()
    except OSError:
        pass


def apply_profile(profile_dir: pathlib.Path, files: dict[pathlib.Path, str], dry_run: bool = False) -> list[pathlib.Path]:
    stale = _stale_managed_skill_files(profile_dir, files)
    changed: list[pathlib.Path] = list(stale)
    for relative_path in sorted(files, key=lambda path: path.as_posix()):
        destination = profile_dir / relative_path
        current = destination.read_text() if destination.is_file() else None
        if current == files[relative_path]:
            continue
        changed.append(relative_path)
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(files[relative_path])
    if not dry_run:
        for relative_path in stale:
            (profile_dir / relative_path).unlink()
        _remove_empty_managed_skill_dirs(profile_dir)
    return sorted(set(changed), key=lambda path: path.as_posix())


def load_desired_state(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Hermes provisioning failed: desired state must be a JSON object")
    if "agents" in payload:
        raise ValueError(
            "Hermes provisioning failed: expected a single-Agent desired state; compile with --agent"
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("desired_state", help="Single-Agent JSON emitted by compile-workspace.py --agent")
    parser.add_argument("profile_dir", help="Hermes profile directory to materialize")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing files")
    args = parser.parse_args()

    try:
        desired_state = load_desired_state(pathlib.Path(args.desired_state))
        files = build_profile_files(desired_state)
        profile_dir = pathlib.Path(args.profile_dir)
        changed = apply_profile(profile_dir, files, dry_run=args.dry_run)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    action = "Would update" if args.dry_run else "Updated"
    if changed:
        print(f"{action} {len(changed)} managed Hermes profile file(s) under {profile_dir}")
        for path in changed:
            print(f" - {path.as_posix()}")
    else:
        print(f"Hermes profile already matches desired state under {profile_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
