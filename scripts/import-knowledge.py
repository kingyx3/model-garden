#!/usr/bin/env python3
"""Stage an owner's UTF-8 business-rules upload as a draft Tier 0 KnowledgeSource.

This edits only one private client workspace. It never grants a Tool, approves a
source, deploys a runtime or claims a real client upload/approval took place.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SLUG = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _knowledge_module():
    spec = importlib.util.spec_from_file_location("modelgarden_import_knowledge_core", ROOT / "platform/core/knowledge.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _regular(path: pathlib.Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    if not path.is_file():
        raise ValueError(f"{label} must be an existing regular file: {path}")
    if path.stat().st_nlink != 1:
        raise ValueError(f"{label} must not be hardlinked: {path}")


def _agent_file(workspace: pathlib.Path, agent: str) -> tuple[pathlib.Path, dict[str, Any]]:
    matches = []
    for path in sorted((workspace / "agents").rglob("agent.yaml")):
        _regular(path, "Agent declaration")
        if not path.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("Agent declaration escapes the client workspace")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("kind") == "Agent" and document.get("metadata", {}).get("name") == agent:
            matches.append((path, document))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one Agent declaration for {agent!r}; found {len(matches)}")
    path, document = matches[0]
    selected = document.get("spec", {}).get("tools", [])
    if not isinstance(selected, list) or "kb.search" not in selected:
        raise ValueError(f"Agent {agent!r} does not select kb.search; import cannot make this file searchable")
    sources = document["spec"].get("knowledge", [])
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        raise ValueError("Agent spec.knowledge must be a list of source names")
    return path, document


def _write_new(path: pathlib.Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def import_knowledge(
    workspace: pathlib.Path,
    source: pathlib.Path,
    *,
    name: str,
    owner: str,
    agent: str,
    classification: str,
    version: str = "1.0.0",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import one bounded local file, selecting it for an Agent but leaving it draft.

    A separately reviewed client-workspace change to spec.status=approved is
    required before the existing compiler/governed kb.search will serve it.
    """
    workspace, source = pathlib.Path(workspace), pathlib.Path(source)
    if not SLUG.fullmatch(name):
        raise ValueError("source name must be a lowercase DNS-like slug")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
        raise ValueError("a named, non-empty owner is required")
    if not SLUG.fullmatch(agent):
        raise ValueError("agent must be a lowercase DNS-like name")
    if classification not in ("public", "internal", "confidential"):
        raise ValueError("choose public, internal or confidential; restricted cannot be served by Tier 0")
    if not VERSION.fullmatch(version):
        raise ValueError("version must be MAJOR.MINOR.PATCH")
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("workspace must be an existing, non-symlink directory")
    knowledge_dir = workspace / "knowledge"
    if knowledge_dir.is_symlink() or not knowledge_dir.is_dir() or not knowledge_dir.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("knowledge directory must be an existing, non-symlink client directory")
    _regular(source, "uploaded file")
    knowledge = _knowledge_module()
    try:
        text = knowledge.read_text_file(source)
        report = knowledge.check_source(text, filename=source.name, classification=classification)
    except (knowledge.KnowledgeError, OSError) as exc:
        raise ValueError(f"knowledge upload rejected: {exc}") from exc

    agent_path, document = _agent_file(workspace, agent)
    content_path = knowledge_dir / f"{name}{source.suffix.lower()}"
    declaration_path = knowledge_dir / f"{name}.yaml"
    resource = {
        "apiVersion": "modelgarden.ai/v1",
        "kind": "KnowledgeSource",
        "metadata": {"name": name, "owner": owner.strip()},
        "spec": {
            "type": "text-file",
            "classification": classification,
            "file": content_path.name,
            "status": "draft",
            "version": version,
            "audiences": [agent],
        },
    }
    output = {
        "name": name,
        "agent": agent,
        "status": "draft",
        "sha256": report["sha256"],
        "sectionCount": report["sectionCount"],
        "warnings": report["warnings"],
        "declaration": str(declaration_path),
        "contentFile": str(content_path),
        "dryRun": dry_run,
        "alreadyImported": False,
        "next": "Review the private client-workspace diff; only the authorized owner may change spec.status to approved before DEV validation/deployment.",
    }

    def check_existing() -> bool:
        if not declaration_path.exists() and not declaration_path.is_symlink() and not content_path.exists() and not content_path.is_symlink():
            return False
        if declaration_path.is_symlink() or content_path.is_symlink():
            raise ValueError("knowledge destination already exists as a symlink")
        if not declaration_path.is_file() or not content_path.is_file():
            raise ValueError("knowledge destination already exists or is partially imported; inspect it manually")
        existing = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict) or any(existing.get(key) != value for key, value in resource.items() if key != "spec"):
            raise ValueError("knowledge source already exists with different metadata")
        spec = existing.get("spec", {})
        if not isinstance(spec, dict) or any(spec.get(key) != value for key, value in resource["spec"].items() if key != "status") or spec.get("status") not in ("draft", "approved"):
            raise ValueError("knowledge source already exists with different settings")
        if content_path.read_bytes() != text.encode("utf-8"):
            raise ValueError("knowledge source already exists with different content; use a separately reviewed version update")
        if name not in document["spec"].get("knowledge", []):
            raise ValueError("knowledge source already exists but Agent selection is missing; inspect it manually")
        return True

    if dry_run:
        output["alreadyImported"] = check_existing()
        return output

    lock_path = knowledge_dir / ".import.lock"
    if lock_path.is_symlink():
        raise ValueError("knowledge import lock must not be a symlink")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise ValueError("knowledge import lock must not be hardlinked")
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Another operator may have changed the Agent while we waited.
        agent_path, document = _agent_file(workspace, agent)
        if check_existing():
            output["alreadyImported"] = True
            return output
        if name in document["spec"].get("knowledge", []):
            raise ValueError("Agent already selects this source name without its files; inspect the workspace")
        document["spec"]["knowledge"] = [*document["spec"].get("knowledge", []), name]
        created: list[pathlib.Path] = []
        try:
            _write_new(content_path, text.encode("utf-8"))
            created.append(content_path)
            _write_new(declaration_path, yaml.safe_dump(resource, sort_keys=False).encode("utf-8"))
            created.append(declaration_path)
            # Publish the Agent update last, atomically, preserving its original permissions.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=agent_path.parent, prefix=".agent-import-", delete=False) as temp:
                temporary = pathlib.Path(temp.name)
                try:
                    yaml.safe_dump(document, temp, sort_keys=False)
                    temp.flush()
                    os.fsync(temp.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.chmod(temporary, stat.S_IMODE(agent_path.stat().st_mode))
                os.replace(temporary, agent_path)
            finally:
                temporary.unlink(missing_ok=True)
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
    finally:
        os.close(fd)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=pathlib.Path, help="existing private client workspace")
    parser.add_argument("file", type=pathlib.Path, help="local owner-supplied .txt or .md")
    parser.add_argument("--name", required=True, help="new KnowledgeSource name")
    parser.add_argument("--owner", required=True, help="named business owner; not an authentication assertion")
    parser.add_argument("--agent", required=True, help="Agent selecting the source")
    parser.add_argument("--classification", required=True, choices=["public", "internal", "confidential"])
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = import_knowledge(args.workspace, args.file, name=args.name, owner=args.owner,
                                  agent=args.agent, classification=args.classification,
                                  version=args.version, dry_run=args.dry_run)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"Knowledge import refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
