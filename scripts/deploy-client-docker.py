#!/usr/bin/env python3
"""Render and optionally deploy one isolated client Hermes runtime with Docker Compose.

This is the intentionally small MVP target adapter. It consumes only generated deployment
outputs (a Hermes profile plus EnvironmentBinding), keeps raw credentials out of those
outputs, resolves logical secret:// references from one deployment-process environment
variable, and injects only the model credential into the Hermes container.

The adapter is designed for a private client repository's protected GitHub Environment
running on a client-labelled self-hosted runner with Docker Engine + Compose. GitHub remains
the engineering control plane; the target host is only a runtime/deployment executor.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
BASE_IMAGE = "python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _load_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _load_hermes_lock(platform_root: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (platform_root / "platform" / "hermes.lock").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    for key in ("repository", "commit", "version"):
        if not values.get(key):
            raise ValueError(f"Docker target failed: Hermes lock is missing {key}")
    return values


def _validate_profile(profile_dir: pathlib.Path) -> str:
    required = (
        profile_dir / "SOUL.md",
        profile_dir / "config.yaml",
        profile_dir / ".modelgarden" / "desired-state.json",
    )
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Docker target failed: missing or unsafe generated profile file {path}")
    for path in profile_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Docker target failed: generated profile may not contain symlinks: {path}")
    config = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Docker target failed: Hermes config.yaml must be a mapping")
    model = config.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("provider"), str) or not model["provider"]:
        raise ValueError("Docker target failed: Hermes config.yaml has no model.provider")
    provider = model["provider"].strip()
    if provider not in PROVIDER_ENV:
        raise ValueError(
            f"Docker target failed: model provider {provider!r} has no approved MVP credential mapping"
        )
    return provider


def _validate_binding(binding: dict[str, Any]) -> tuple[str, str]:
    if binding.get("apiVersion") != "modelgarden.ai/v1" or binding.get("kind") != "EnvironmentBinding":
        raise ValueError("Docker target failed: expected modelgarden.ai/v1 EnvironmentBinding")
    spec = binding.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("Docker target failed: EnvironmentBinding.spec must be an object")
    environment = spec.get("environment")
    if not isinstance(environment, str) or not environment:
        raise ValueError("Docker target failed: EnvironmentBinding has no environment")
    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("Docker target failed: EnvironmentBinding has no runtime object")
    credential_ref = runtime.get("model_credential_ref")
    if not isinstance(credential_ref, str) or not credential_ref.startswith("secret://") or len(credential_ref) <= len("secret://"):
        raise ValueError(
            "Docker target failed: runtime.model_credential_ref must be a non-empty secret:// reference"
        )
    return environment, credential_ref


def _collect_secret_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            refs.update(_collect_secret_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_collect_secret_refs(child))
    elif isinstance(value, str) and value.startswith("secret://"):
        refs.add(value)
    return refs


def resolve_secrets(binding: dict[str, Any], encoded: str | None) -> dict[str, str]:
    """Resolve exactly the references present in this binding, rejecting over-broad input."""
    refs = _collect_secret_refs(binding)
    if not refs:
        return {}
    if not encoded:
        raise ValueError("Docker target failed: MODEL_GARDEN_RUNTIME_SECRETS_JSON is required")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("Docker target failed: runtime secrets JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("Docker target failed: runtime secrets JSON must be an object")
    supplied = set(value)
    missing = sorted(refs - supplied)
    extra = sorted(supplied - refs)
    if missing:
        raise ValueError(f"Docker target failed: unresolved secret references: {', '.join(missing)}")
    if extra:
        raise ValueError(f"Docker target failed: runtime secret map contains unreferenced keys: {', '.join(extra)}")
    resolved: dict[str, str] = {}
    for ref in sorted(refs):
        secret = value.get(ref)
        if not isinstance(secret, str) or not secret:
            raise ValueError(f"Docker target failed: {ref} must resolve to a non-empty string")
        resolved[ref] = secret
    return resolved


def _entrypoint() -> str:
    return """#!/bin/sh
set -eu
mkdir -p "$HERMES_HOME/.modelgarden" "$HERMES_HOME/skills"
cp /opt/profile-seed/SOUL.md "$HERMES_HOME/SOUL.md"
cp /opt/profile-seed/config.yaml "$HERMES_HOME/config.yaml"
cp /opt/profile-seed/.modelgarden/desired-state.json "$HERMES_HOME/.modelgarden/desired-state.json"
rm -rf "$HERMES_HOME/skills/model-garden"
if [ -d /opt/profile-seed/skills/model-garden ]; then
  mkdir -p "$HERMES_HOME/skills"
  cp -R /opt/profile-seed/skills/model-garden "$HERMES_HOME/skills/model-garden"
fi
exec "$@"
"""


def _dockerfile(lock: dict[str, str]) -> str:
    return f"""FROM {BASE_IMAGE}
ARG HERMES_VERSION={lock['version']}
ARG HERMES_COMMIT={lock['commit']}
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    HERMES_HOME=/opt/data
COPY hermes-source /opt/hermes-agent
RUN test \"$(git -C /opt/hermes-agent rev-parse HEAD 2>/dev/null || true)\" = \"$HERMES_COMMIT\" || \\
      test \"$(cat /opt/hermes-agent/.model-garden-hermes-commit)\" = \"$HERMES_COMMIT\" && \\
    python -m pip install --no-cache-dir -e /opt/hermes-agent && \\
    test \"$(python -c 'import importlib.metadata; print(importlib.metadata.version(\"hermes-agent\"))')\" = \"$HERMES_VERSION\"
COPY profile /opt/profile-seed
COPY entrypoint.sh /usr/local/bin/model-garden-entrypoint
RUN chmod 0555 /usr/local/bin/model-garden-entrypoint
ENTRYPOINT [\"/usr/local/bin/model-garden-entrypoint\"]
CMD [\"hermes\", \"serve\", \"--host\", \"127.0.0.1\", \"--port\", \"9119\", \"--skip-build\"]
"""


def _compose(project_name: str, provider: str, lock: dict[str, str]) -> str:
    provider_env = PROVIDER_ENV[provider]
    document = {
        "services": {
            "hermes": {
                "build": {
                    "context": ".",
                    "args": {
                        "HERMES_VERSION": lock["version"],
                        "HERMES_COMMIT": lock["commit"],
                    },
                },
                "image": f"model-garden-{project_name}:current",
                "restart": "unless-stopped",
                "environment": {
                    "HERMES_HOME": "/opt/data",
                    provider_env: "${MODEL_GARDEN_MODEL_CREDENTIAL:?MODEL_GARDEN_MODEL_CREDENTIAL is required}",
                },
                "volumes": ["hermes-data:/opt/data"],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import socket; s=socket.create_connection(('127.0.0.1',9119),5); s.close()",
                    ],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 6,
                    "start_period": "20s",
                },
            }
        },
        "volumes": {"hermes-data": {}},
    }
    return yaml.safe_dump(document, sort_keys=False)


def _clone_hermes(lock: dict[str, str], destination: pathlib.Path) -> None:
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--filter=blob:none", lock["repository"], str(destination)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(destination), "checkout", "--quiet", lock["commit"]],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        actual = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        raise ValueError(f"Docker target failed: cannot fetch pinned Hermes revision: {detail}") from exc
    if actual != lock["commit"]:
        raise ValueError(f"Docker target failed: Hermes checkout mismatch: expected {lock['commit']}, got {actual}")
    shutil.rmtree(destination / ".git", ignore_errors=True)
    (destination / ".model-garden-hermes-commit").write_text(lock["commit"] + "\n", encoding="utf-8")


def render_bundle(
    profile_dir: pathlib.Path,
    binding_path: pathlib.Path,
    bundle_dir: pathlib.Path,
    project_name: str,
    *,
    platform_root: pathlib.Path = ROOT,
    fetch_source: bool = False,
) -> dict[str, Any]:
    if not PROJECT_NAME.fullmatch(project_name):
        raise ValueError("Docker target failed: project name must be lowercase letters, numbers, and internal hyphens")
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise ValueError(f"Docker target failed: refusing to overwrite non-empty bundle directory {bundle_dir}")
    provider = _validate_profile(profile_dir)
    binding = _load_json(binding_path, "EnvironmentBinding")
    environment, credential_ref = _validate_binding(binding)
    lock = _load_hermes_lock(platform_root)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(profile_dir, bundle_dir / "profile")
    normalized_binding = json.dumps(binding, indent=2, sort_keys=True) + "\n"
    (bundle_dir / "environment-binding.json").write_text(normalized_binding, encoding="utf-8")
    (bundle_dir / "entrypoint.sh").write_text(_entrypoint(), encoding="utf-8")
    (bundle_dir / "Dockerfile").write_text(_dockerfile(lock), encoding="utf-8")
    (bundle_dir / "compose.yaml").write_text(_compose(project_name, provider, lock), encoding="utf-8")
    metadata = {
        "apiVersion": "modelgarden.ai/v1",
        "kind": "DockerDeploymentBundle",
        "project": project_name,
        "environment": environment,
        "provider": provider,
        "modelCredentialRef": credential_ref,
        "hermes": {"version": lock["version"], "revision": lock["commit"]},
        "bindingSha256": hashlib.sha256(normalized_binding.encode("utf-8")).hexdigest(),
    }
    (bundle_dir / "deployment-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if fetch_source:
        _clone_hermes(lock, bundle_dir / "hermes-source")
    return metadata


def _run(command: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def _wait_healthy(project_name: str, bundle_dir: pathlib.Path, env: dict[str, str], timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    compose = ["docker", "compose", "-p", project_name, "-f", str(bundle_dir / "compose.yaml")]
    while time.monotonic() < deadline:
        container = _run([*compose, "ps", "-q", "hermes"], env=env, check=False).stdout.strip()
        if container:
            status = _run(
                [
                    "docker", "inspect", "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                    container,
                ],
                env=env,
                check=False,
            ).stdout.strip()
            if status == "healthy":
                return True
            if status in {"exited", "dead", "unhealthy"}:
                return False
        time.sleep(2)
    return False


def apply_bundle(
    bundle_dir: pathlib.Path,
    binding: dict[str, Any],
    project_name: str,
    *,
    secrets_json: str | None,
    timeout: int = 90,
) -> None:
    _, model_ref = _validate_binding(binding)
    resolved = resolve_secrets(binding, secrets_json)
    model_secret = resolved[model_ref]

    runtime_env = os.environ.copy()
    runtime_env.pop("MODEL_GARDEN_RUNTIME_SECRETS_JSON", None)
    runtime_env["MODEL_GARDEN_MODEL_CREDENTIAL"] = model_secret
    compose = ["docker", "compose", "-p", project_name, "-f", str(bundle_dir / "compose.yaml")]
    image = f"model-garden-{project_name}:current"
    rollback = f"model-garden-{project_name}:rollback"

    _run(["docker", "compose", "version"], env=runtime_env)
    had_previous = _run(["docker", "image", "inspect", image], env=runtime_env, check=False).returncode == 0
    if had_previous:
        _run(["docker", "tag", image, rollback], env=runtime_env)

    try:
        _run([*compose, "build"], env=runtime_env)
        _run([*compose, "up", "-d", "--remove-orphans"], env=runtime_env)
        if not _wait_healthy(project_name, bundle_dir, runtime_env, timeout):
            raise RuntimeError("candidate Hermes container did not become healthy")
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        if had_previous:
            _run(["docker", "tag", rollback, image], env=runtime_env, check=False)
            _run([*compose, "up", "-d", "--no-build", "--force-recreate"], env=runtime_env, check=False)
            restored = _wait_healthy(project_name, bundle_dir, runtime_env, timeout)
            suffix = "; previous runtime restored" if restored else "; rollback also failed health verification"
        else:
            _run([*compose, "down"], env=runtime_env, check=False)
            suffix = "; no previous runtime existed"
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        raise ValueError(f"Docker target failed: deployment verification failed: {detail}{suffix}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_dir", type=pathlib.Path, help="generated Hermes profile directory")
    parser.add_argument("environment_binding", type=pathlib.Path, help="validated EnvironmentBinding JSON")
    parser.add_argument("bundle_dir", type=pathlib.Path, help="empty directory for the Docker deployment bundle")
    parser.add_argument("--project-name", required=True, help="stable lowercase client-environment deployment name")
    parser.add_argument("--apply", action="store_true", help="build/update the target runtime using Docker Compose")
    parser.add_argument("--health-timeout", type=int, default=90, help="seconds to wait for a healthy Hermes container")
    args = parser.parse_args()

    try:
        metadata = render_bundle(
            args.profile_dir,
            args.environment_binding,
            args.bundle_dir,
            args.project_name,
            fetch_source=args.apply,
        )
        if args.apply:
            binding = _load_json(args.environment_binding, "EnvironmentBinding")
            apply_bundle(
                args.bundle_dir,
                binding,
                args.project_name,
                secrets_json=os.environ.get("MODEL_GARDEN_RUNTIME_SECRETS_JSON"),
                timeout=args.health_timeout,
            )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    action = "Deployed" if args.apply else "Rendered"
    print(
        f"{action} Docker target {metadata['project']} ({metadata['environment']}) "
        f"with Hermes {metadata['hermes']['version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
