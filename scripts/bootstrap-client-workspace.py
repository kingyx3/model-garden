#!/usr/bin/env python3
"""Create the minimal portable client workspace for a Model Garden engagement.

The generated workspace contains client-owned desired state only. It deliberately does
not copy Model Garden platform code, reusable Skills, connectors, or credentials. The
lockfile pins the Model Garden/Hermes versions from the checkout used to bootstrap it.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLIENT_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _platform_versions(root: pathlib.Path) -> tuple[str, str, str]:
    modelgarden = (root / "VERSION").read_text(encoding="utf-8").strip()
    hermes_values: dict[str, str] = {}
    for line in (root / "platform" / "hermes.lock").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            hermes_values[key.strip()] = value.strip()
    version = hermes_values.get("version")
    revision = hermes_values.get("commit")
    if not modelgarden or not version or not revision:
        raise ValueError("Model Garden or Hermes version metadata is incomplete")
    return modelgarden, version, revision


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def bootstrap(output: pathlib.Path, client_slug: str, root: pathlib.Path = ROOT) -> pathlib.Path:
    if not CLIENT_SLUG.fullmatch(client_slug):
        raise ValueError("client slug must use lowercase letters, numbers, and internal hyphens only")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty directory: {output}")
    output.mkdir(parents=True, exist_ok=True)

    modelgarden_version, hermes_version, hermes_revision = _platform_versions(root)

    _write(
        output / "modelgarden.yaml",
        f"""workspace:\n  client: {client_slug}\n  contract: modelgarden.ai/v1\n  agents:\n    - receptionist\n""",
    )
    _write(
        output / "platform.lock.yaml",
        f"""modelgarden: {modelgarden_version}\ncontracts: v1\nhermes: {hermes_version}\nhermes_revision: {hermes_revision}\n""",
    )
    _write(
        output / "agents" / "receptionist" / "agent.yaml",
        f"""apiVersion: modelgarden.ai/v1\nkind: Agent\nmetadata:\n  name: receptionist\n  owner: {client_slug}\n  description: Client receptionist bootstrap; add only approved business capabilities.\nspec:\n  model:\n    profile: reasoning.high\n  instructions:\n    file: instructions.md\n  skills: []\n  tools: []\n  knowledge: []\n  security:\n    dataClassification: internal\n  approvals:\n    externalActions: required\n""",
    )
    _write(
        output / "agents" / "receptionist" / "instructions.md",
        f"""# Receptionist\n\nYou are the front-office receptionist for {client_slug}.\n\n- Use only approved business knowledge and procedures.\n- Collect only the minimum information needed for the caller's request.\n- Never claim an external action succeeded until its Tool reports success.\n- Respect approval and permission boundaries for every external action.\n- Hand off to a human when safe completion is not possible.\n\nReplace this bootstrap text with business-owner-approved instructions before production.\n""",
    )
    _write(
        output / "model-profiles" / "general.yaml",
        f"""apiVersion: modelgarden.ai/v1\nkind: ModelProfile\nmetadata:\n  name: reasoning.high\n  owner: {client_slug}\n  description: Bootstrap routing profile; use approved deployment model aliases.\nspec:\n  candidates:\n    - provider: openai\n      model: approved-openai-model\n      priority: 1\n    - provider: anthropic\n      model: approved-anthropic-model\n      priority: 2\n    - provider: internal\n      model: approved-open-weight-model\n      priority: 3\n  constraints:\n    maxDataClassification: confidential\n  routing:\n    strategy: ordered-fallback\n""",
    )
    _write(output / "skills" / "README.md", "# Client-specific Skills\n\nAdd only genuinely client-specific Skills here. Prefer pinned curated Model Garden dependencies when available.\n")
    _write(output / "knowledge" / "sources.yaml", "sources: []\n")
    _write(output / "evals" / "receptionist" / "calls.yaml", "scenarios: []\n")
    for environment in ("dev", "prod"):
        _write(
            output / "environments" / f"{environment}.yaml",
            f"""environment: {environment}\nruntime:\n  profile: receptionist\n""",
        )
    _write(
        output / "README.md",
        f"""# {client_slug} AI workspace\n\nThis private repository is the portable source of truth for {client_slug}'s business-specific Model Garden desired state.\n\n## Bootstrap pins\n\n- Model Garden: `{modelgarden_version}`\n- Hermes: `{hermes_version}` (`{hermes_revision}`)\n- Contract: `v1`\n\n## Next steps\n\n1. Replace the Receptionist bootstrap instructions with business-owner-approved behaviour.\n2. Add approved knowledge references and representative evals.\n3. Select curated Skills/Tools only when the pinned Model Garden compiler can resolve them.\n4. Keep provider credentials in GitHub `dev`/`prod` Environment secrets or provider-native authorization flows, never in this repository.\n5. Validate and compile this workspace with the pinned Model Garden release before promotion.\n\nDo not copy Model Garden platform code, generic connectors, generated Hermes state, or raw credentials into this workspace.\n""",
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_slug", help="DNS/repository-safe client identifier")
    parser.add_argument("--output", type=pathlib.Path, help="destination directory; defaults to <client>-ai-workspace")
    args = parser.parse_args()
    output = args.output or pathlib.Path(f"{args.client_slug}-ai-workspace")
    try:
        created = bootstrap(output, args.client_slug)
    except (OSError, ValueError) as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(f"Created Model Garden client workspace at {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
