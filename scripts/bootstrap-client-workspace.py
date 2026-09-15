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


def _client_workflow(client_slug: str) -> str:
    return """name: Model Garden client runtime

on:
  pull_request:
  push:
    branches:
      - dev
      - main

permissions:
  contents: read

concurrency:
  group: model-garden-${{ github.ref }}
  cancel-in-progress: false

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - name: Resolve locked Model Garden release
        shell: bash
        run: |
          set -euo pipefail
          version="$(awk '$1 == \"modelgarden:\" { print $2; exit }' platform.lock.yaml)"
          if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "platform.lock.yaml must pin modelgarden as MAJOR.MINOR.PATCH" >&2
            exit 1
          fi
          git clone --depth 1 --branch "v${version}" --single-branch \
            https://github.com/kingyx3/model-garden.git "$RUNNER_TEMP/model-garden"
          echo "MODEL_GARDEN_ROOT=$RUNNER_TEMP/model-garden" >> "$GITHUB_ENV"
      - name: Install validation dependencies
        run: python3 -m pip install -r "$MODEL_GARDEN_ROOT/requirements-dev.txt"
      - name: Validate client workspace
        run: python3 "$MODEL_GARDEN_ROOT/scripts/validate-workspace.py" "$GITHUB_WORKSPACE"
      - name: Verify locked runtime rebuild
        run: >-
          python3 "$MODEL_GARDEN_ROOT/scripts/rebuild-client-runtime.py"
          "$GITHUB_WORKSPACE" "$RUNNER_TEMP/hermes-profile"
          --agent receptionist --dry-run

  materialize:
    if: github.event_name == 'push'
    needs: validate
    runs-on: ubuntu-latest
    environment:
      name: ${{ github.ref_name == 'main' && 'prod' || 'dev' }}
    steps:
      - uses: actions/checkout@v7
      - name: Resolve locked Model Garden release
        shell: bash
        run: |
          set -euo pipefail
          version="$(awk '$1 == \"modelgarden:\" { print $2; exit }' platform.lock.yaml)"
          if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "platform.lock.yaml must pin modelgarden as MAJOR.MINOR.PATCH" >&2
            exit 1
          fi
          git clone --depth 1 --branch "v${version}" --single-branch \
            https://github.com/kingyx3/model-garden.git "$RUNNER_TEMP/model-garden"
          echo "MODEL_GARDEN_ROOT=$RUNNER_TEMP/model-garden" >> "$GITHUB_ENV"
      - name: Install runtime rebuild dependencies
        run: python3 -m pip install -r "$MODEL_GARDEN_ROOT/requirements-dev.txt"
      - name: Render validated environment binding
        run: >-
          python3 "$MODEL_GARDEN_ROOT/scripts/render-client-environment.py"
          "$GITHUB_WORKSPACE/environments/${{ github.ref_name == 'main' && 'prod' || 'dev' }}.yaml"
          --environment "${{ github.ref_name == 'main' && 'prod' || 'dev' }}"
          --output "$RUNNER_TEMP/environment-binding.json"
      - name: Materialize locked Hermes runtime profile
        run: >-
          python3 "$MODEL_GARDEN_ROOT/scripts/rebuild-client-runtime.py"
          "$GITHUB_WORKSPACE" "$RUNNER_TEMP/hermes-profile"
          --agent receptionist
      - name: Upload reproducible deployment inputs
        uses: actions/upload-artifact@v4
        with:
          name: model-garden-deployment-${{ github.ref_name }}-${{ github.sha }}
          path: |
            ${{ runner.temp }}/hermes-profile
            ${{ runner.temp }}/environment-binding.json
          if-no-files-found: error
          retention-days: 7

  deploy:
    if: github.event_name == 'push' && vars.MODEL_GARDEN_DOCKER_RUNNER != ''
    needs: materialize
    runs-on:
      - self-hosted
      - ${{ vars.MODEL_GARDEN_DOCKER_RUNNER }}
    environment:
      name: ${{ github.ref_name == 'main' && 'prod' || 'dev' }}
    env:
      MODEL_GARDEN_RUNTIME_SECRETS_JSON: ${{ secrets.MODEL_GARDEN_RUNTIME_SECRETS_JSON }}
    steps:
      - uses: actions/checkout@v7
      - name: Download reproducible deployment inputs
        uses: actions/download-artifact@v4
        with:
          name: model-garden-deployment-${{ github.ref_name }}-${{ github.sha }}
          path: ${{ runner.temp }}/model-garden-deployment
      - name: Resolve locked Model Garden release
        shell: bash
        run: |
          set -euo pipefail
          rm -rf "$RUNNER_TEMP/model-garden" "$RUNNER_TEMP/model-garden-target" "$RUNNER_TEMP/model-garden-deploy-venv"
          version="$(awk '$1 == \"modelgarden:\" { print $2; exit }' platform.lock.yaml)"
          if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "platform.lock.yaml must pin modelgarden as MAJOR.MINOR.PATCH" >&2
            exit 1
          fi
          git clone --depth 1 --branch "v${version}" --single-branch \
            https://github.com/kingyx3/model-garden.git "$RUNNER_TEMP/model-garden"
          python3 -m venv "$RUNNER_TEMP/model-garden-deploy-venv"
          "$RUNNER_TEMP/model-garden-deploy-venv/bin/python" -m pip install --disable-pip-version-check --quiet \
            -r "$RUNNER_TEMP/model-garden/requirements-dev.txt"
      - name: Deploy and verify isolated Docker runtime
        shell: bash
        run: >-
          "$RUNNER_TEMP/model-garden-deploy-venv/bin/python"
          "$RUNNER_TEMP/model-garden/scripts/deploy-client-docker.py"
          "$RUNNER_TEMP/model-garden-deployment/hermes-profile"
          "$RUNNER_TEMP/model-garden-deployment/environment-binding.json"
          "$RUNNER_TEMP/model-garden-target"
          --project-name "__CLIENT_SLUG__-${{ github.ref_name == 'main' && 'prod' || 'dev' }}"
          --apply
""".replace("__CLIENT_SLUG__", client_slug)


def bootstrap(output: pathlib.Path, client_slug: str, root: pathlib.Path = ROOT) -> pathlib.Path:
    if not CLIENT_SLUG.fullmatch(client_slug):
        raise ValueError("client slug must use lowercase letters, numbers, and internal hyphens only")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty directory: {output}")
    output.mkdir(parents=True, exist_ok=True)

    modelgarden_version, hermes_version, hermes_revision = _platform_versions(root)

    _write(output / "modelgarden.yaml", f"""workspace:\n  client: {client_slug}\n  contract: modelgarden.ai/v1\n  agents:\n    - receptionist\n""")
    _write(output / "platform.lock.yaml", f"""modelgarden: {modelgarden_version}\ncontracts: v1\nhermes: {hermes_version}\nhermes_revision: {hermes_revision}\n""")
    _write(output / "agents" / "receptionist" / "agent.yaml", f"""apiVersion: modelgarden.ai/v1\nkind: Agent\nmetadata:\n  name: receptionist\n  owner: {client_slug}\n  description: Client receptionist bootstrap; add only approved business capabilities.\nspec:\n  model:\n    profile: reasoning.high\n  instructions:\n    file: instructions.md\n  skills: []\n  tools: []\n  knowledge: []\n  security:\n    dataClassification: internal\n  approvals:\n    externalActions: required\n""")
    _write(output / "agents" / "receptionist" / "instructions.md", f"""# Receptionist\n\nYou are the front-office receptionist for {client_slug}.\n\n- Use only approved business knowledge and procedures.\n- Collect only the minimum information needed for the caller's request.\n- Never claim an external action succeeded until its Tool reports success.\n- Respect approval and permission boundaries for every external action.\n- Hand off to a human when safe completion is not possible.\n\nReplace this bootstrap text with business-owner-approved instructions before production.\n""")
    _write(output / "model-profiles" / "general.yaml", f"""apiVersion: modelgarden.ai/v1\nkind: ModelProfile\nmetadata:\n  name: reasoning.high\n  owner: {client_slug}\n  description: Bootstrap routing profile; use approved deployment model aliases.\nspec:\n  candidates:\n    - provider: openai\n      model: approved-openai-model\n      priority: 1\n    - provider: anthropic\n      model: approved-anthropic-model\n      priority: 2\n    - provider: internal\n      model: approved-open-weight-model\n      priority: 3\n  constraints:\n    maxDataClassification: confidential\n  routing:\n    strategy: ordered-fallback\n""")
    _write(output / "skills" / "README.md", "# Client-specific Skills\n\nAdd only genuinely client-specific Skills here. Prefer pinned curated Model Garden dependencies when available.\n")
    _write(output / "knowledge" / "sources.yaml", "sources: []\n")
    _write(output / "evals" / "receptionist" / "calls.yaml", "scenarios: []\n")
    for environment in ("dev", "prod"):
        _write(output / "environments" / f"{environment}.yaml", f"""environment: {environment}\nruntime:\n  profile: receptionist\n  model_credential_ref: secret://model/{environment}\n""")
    _write(output / ".github" / "workflows" / "model-garden.yml", _client_workflow(client_slug))
    _write(output / "README.md", f"""# {client_slug} AI workspace\n\nThis private repository is the portable source of truth for {client_slug}'s business-specific Model Garden desired state.\n\n## Bootstrap pins\n\n- Model Garden: `{modelgarden_version}`\n- Hermes: `{hermes_version}` (`{hermes_revision}`)\n- Contract: `v1`\n\n## GitHub delivery path\n\nThe generated `.github/workflows/model-garden.yml` validates pull requests against the exact Model Garden release pinned in `platform.lock.yaml`. Pushes to `dev` or `main` run through the corresponding GitHub Environment, render the selected non-secret environment binding, and materialize the reproducible Hermes profile. Both are uploaded together as short-lived deployment inputs.\n\nFor the MVP Docker target, configure a private self-hosted GitHub Actions runner with Docker Engine + the Compose plugin and give it a client-specific label. Set repository/environment variable `MODEL_GARDEN_DOCKER_RUNNER` to that label. The deploy job is skipped when the variable is absent, preserving artifact-only operation.\n\nEach `dev`/`prod` GitHub Environment supplies one secret named `MODEL_GARDEN_RUNTIME_SECRETS_JSON`. It must be a JSON object whose keys exactly match the `secret://` references in that environment file; missing or extra keys fail closed. For the generated bootstrap the minimum shapes are `{{\"secret://model/dev\":\"<dev model credential>\"}}` and `{{\"secret://model/prod\":\"<prod model credential>\"}}`. The aggregate map exists only in the deployment process; the Docker adapter removes it before invoking Docker and passes only the selected model credential to Hermes.\n\n## Next steps\n\n1. Replace the Receptionist bootstrap instructions with business-owner-approved behaviour.\n2. Add approved knowledge references and representative evals.\n3. Select curated Skills/Tools only when the pinned Model Garden compiler can resolve them.\n4. Create protected GitHub `dev` and `prod` Environments and configure the environment-scoped runtime secret map; never store credentials in this repository.\n5. For managed deployment, attach the private client-specific Docker runner label through `MODEL_GARDEN_DOCKER_RUNNER`.\n6. Use pull requests for validation, merge approved integration changes to `dev`, then promote the tested revision to `main` for production.\n\nDo not copy Model Garden platform code, generic connectors, generated Hermes state, or raw credentials into this workspace.\n""")
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
