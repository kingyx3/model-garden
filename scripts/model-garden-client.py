#!/usr/bin/env python3
"""Small operator CLI for Model Garden client workspaces.

The CLI intentionally wraps existing, tested platform primitives instead of introducing a
control plane. `init` creates the canonical secret-free client workspace and can choose a
bounded initial employee role without forking or copying Model Garden platform code.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "scripts" / "bootstrap-client-workspace.py"
ROLE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

spec = importlib.util.spec_from_file_location("bootstrap_client_workspace", BOOTSTRAP_PATH)
bootstrapper = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bootstrapper)


def _role_title(role: str) -> str:
    return " ".join(part.capitalize() for part in role.split("-"))


def _replace(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(old, new), encoding="utf-8")


def init_client(output: pathlib.Path, client_slug: str, role: str = "receptionist") -> pathlib.Path:
    """Create a canonical workspace, optionally replacing the default Receptionist role."""
    if not ROLE_SLUG.fullmatch(role):
        raise ValueError("role must use lowercase letters, numbers, and internal hyphens only")

    created = bootstrapper.bootstrap(output, client_slug)
    if role == "receptionist":
        return created

    old_agent = created / "agents" / "receptionist"
    new_agent = created / "agents" / role
    old_agent.rename(new_agent)

    old_evals = created / "evals" / "receptionist"
    new_evals = created / "evals" / role
    old_evals.rename(new_evals)

    title = _role_title(role)
    (new_agent / "agent.yaml").write_text(
        f"""apiVersion: modelgarden.ai/v1\nkind: Agent\nmetadata:\n  name: {role}\n  owner: {client_slug}\n  description: Client {role} bootstrap; add only approved business capabilities.\nspec:\n  model:\n    profile: reasoning.high\n  instructions:\n    file: instructions.md\n  skills: []\n  tools: []\n  knowledge: []\n  security:\n    dataClassification: internal\n  approvals:\n    externalActions: required\n""",
        encoding="utf-8",
    )
    (new_agent / "instructions.md").write_text(
        f"""# {title}\n\nYou are the {title.lower()} for {client_slug}.\n\n- Perform only the bounded responsibilities approved for this pilot.\n- Use only approved business knowledge and procedures.\n- Collect only the minimum information needed for the current task.\n- Never claim an external action succeeded until its Tool reports success.\n- Respect approval and permission boundaries for every external action.\n- Hand off to a human when safe completion is not possible.\n\nReplace this bootstrap text with business-owner-approved instructions before production.\n""",
        encoding="utf-8",
    )

    for path in (
        created / "modelgarden.yaml",
        created / "environments" / "dev.yaml",
        created / "environments" / "prod.yaml",
        created / ".github" / "workflows" / "model-garden.yml",
        created / "README.md",
    ):
        _replace(path, "receptionist", role)
        _replace(path, "Receptionist", title)

    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create a new secret-free client workspace")
    init_parser.add_argument("client_slug", help="DNS/repository-safe client identifier")
    init_parser.add_argument("--role", default="receptionist", help="initial employee role slug")
    init_parser.add_argument("--output", type=pathlib.Path, help="destination; defaults to <client>-ai-workspace")
    args = parser.parse_args()

    if args.command == "init":
        output = args.output or pathlib.Path(f"{args.client_slug}-ai-workspace")
        try:
            created = init_client(output, args.client_slug, args.role)
        except (OSError, ValueError) as exc:
            print(f"Client init failed: {exc}", file=sys.stderr)
            return 1
        print(f"Created Model Garden client workspace at {created}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
