#!/usr/bin/env python3
"""Thin operator helper for repeatable Model Garden client setup and operations.

This is intentionally not a control plane. It wraps the existing Git-backed bootstrap,
GitHub Environment setup, readiness checks, and exact runtime approval operations so an
operator does not need to remember secret-map syntax, Docker internals, or generated
workflow details.
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROJECT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
KEYLESS_GCP_VARIABLES = {
    "MODEL_GARDEN_CLOUD_PROVIDER",
    "MODEL_GARDEN_CLOUD_READY",
    "MODEL_GARDEN_INFRA_OWNERSHIP",
    "MODEL_GARDEN_TF_STATE_BUCKET",
    "GCP_PROJECT_ID",
    "GCP_REGION",
    "GCP_ZONE",
    "GCP_WORKLOAD_IDENTITY_PROVIDER",
    "GCP_DEPLOY_SERVICE_ACCOUNT",
    "GCP_RUNTIME_SERVICE_ACCOUNT",
}


def _load_script(module_name: str, filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


def _run(
    command: list[str],
    *,
    cwd: pathlib.Path | None = None,
    input_text: str | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=capture,
        check=False,
    )


def _require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise ValueError(f"required command is not installed or not on PATH: {name}")


def _load_yaml(path: pathlib.Path, label: str) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a YAML mapping: {path}")
    return value


def _workspace_agents(workspace: pathlib.Path) -> list[str]:
    document = _load_yaml(workspace / "modelgarden.yaml", "modelgarden.yaml")
    config = document.get("workspace")
    if not isinstance(config, dict):
        raise ValueError("modelgarden.yaml must contain workspace mapping")
    agents = config.get("agents")
    if not isinstance(agents, list) or not agents or not all(isinstance(value, str) and value for value in agents):
        raise ValueError("modelgarden.yaml workspace.agents must be a non-empty string list")
    return agents


def _secret_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            refs.update(_secret_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_secret_refs(child))
    elif isinstance(value, str) and value.startswith("secret://"):
        refs.add(value)
    return refs


def _validate_repo(repo: str) -> None:
    if not REPO.fullmatch(repo):
        raise ValueError("GitHub repository must be owner/name")


def initialize(
    workspace: pathlib.Path,
    client_slug: str,
    agent_slug: str,
    role_title: str | None,
    github_repo: str | None,
) -> None:
    bootstrapper = _load_script("bootstrap_client_workspace", "bootstrap-client-workspace.py")
    bootstrapper.bootstrap(workspace, client_slug, agent_slug, role_title)
    print(f"Created {workspace} with initial Agent {agent_slug!r}.")
    if github_repo is None:
        print("Next: create a private GitHub repo, push this workspace, then run client-operator.py configure.")
        return

    _validate_repo(github_repo)
    _require_command("git")
    _require_command("gh")
    commands = [
        (["git", "init", "-b", "main"], workspace),
        (["git", "add", "."], workspace),
        (["git", "commit", "-m", f"Bootstrap {client_slug} Model Garden workspace"], workspace),
        (["gh", "repo", "create", github_repo, "--private", "--source", ".", "--remote", "origin", "--push"], workspace),
        (["git", "checkout", "-b", "dev"], workspace),
        (["git", "push", "-u", "origin", "dev"], workspace),
        (["git", "checkout", "main"], workspace),
    ]
    for command, cwd in commands:
        result = _run(command, cwd=cwd)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise ValueError(f"{' '.join(command[:3])} failed: {detail}")
    print(f"Created private GitHub workspace {github_repo} with main and dev branches.")


def configure(
    workspace: pathlib.Path,
    repo: str,
    environment: str,
    runner_label: str | None = None,
    *,
    dry_run: bool = False,
) -> None:
    """Configure runtime secrets; optionally configure the legacy self-hosted Docker runner."""
    _validate_repo(repo)
    renderer = _load_script("render_client_environment", "render-client-environment.py")
    rendered = renderer.render(workspace / "environments" / f"{environment}.yaml", environment)
    refs = sorted(_secret_refs(rendered))
    if not refs:
        raise ValueError(f"environment {environment!r} has no secret:// references to configure")
    if runner_label is not None and not runner_label.strip():
        raise ValueError("runner label must be non-empty when supplied")

    print(f"GitHub environment: {repo} / {environment}")
    print("Required runtime secret references:")
    for ref in refs:
        print(f"  - {ref}")
    if runner_label:
        print(f"Deployment path: self-hosted Docker runner {runner_label}")
    else:
        print("Deployment path: no self-hosted runner requested; use the preferred keyless cloud bootstrap or artifact-only mode.")
    if dry_run:
        print("Dry run only; no GitHub settings or secrets were changed.")
        return

    _require_command("gh")
    create_env = _run(["gh", "api", "--method", "PUT", f"repos/{repo}/environments/{environment}", "--silent"])
    if create_env.returncode != 0:
        raise ValueError(f"cannot create/read GitHub Environment {environment}: {create_env.stderr.strip()}")

    values: dict[str, str] = {}
    for ref in refs:
        value = getpass.getpass(f"Value for {ref}: ")
        if not value:
            raise ValueError(f"{ref} must resolve to a non-empty value")
        values[ref] = value
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n"
    secret = _run(
        [
            "gh",
            "secret",
            "set",
            "MODEL_GARDEN_RUNTIME_SECRETS_JSON",
            "--env",
            environment,
            "--repo",
            repo,
        ],
        input_text=payload,
    )
    if secret.returncode != 0:
        raise ValueError(f"cannot set runtime secret map: {secret.stderr.strip()}")

    if runner_label:
        variable = _run(
            [
                "gh",
                "variable",
                "set",
                "MODEL_GARDEN_DOCKER_RUNNER",
                "--env",
                environment,
                "--repo",
                repo,
                "--body",
                runner_label,
            ]
        )
        if variable.returncode != 0:
            raise ValueError(f"cannot set Docker runner variable: {variable.stderr.strip()}")

    print(f"Configured {environment} without writing runtime credentials to the workspace.")
    if runner_label is None:
        print("For the preferred GCP path, run scripts/bootstrap-cloud.py once for this repository after both environments are configured.")
    if environment == "prod":
        print("Review the GitHub prod Environment protection/reviewer policy before production traffic.")


def _check_command(command: list[str]) -> tuple[bool, str]:
    result = _run(command)
    if result.returncode == 0:
        text = result.stdout.strip().splitlines()
        return True, text[0] if text else "ok"
    return False, result.stderr.strip() or result.stdout.strip() or "failed"


def _parse_gh_variables(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, list):
        return {}
    values: dict[str, str] = {}
    for item in payload:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str):
            values[item["name"]] = item["value"]
    return values


def _keyless_gcp_ready(variables: dict[str, str]) -> tuple[bool, str]:
    missing = sorted(name for name in KEYLESS_GCP_VARIABLES if not variables.get(name))
    if variables.get("MODEL_GARDEN_CLOUD_PROVIDER") != "gcp":
        return False, "cloud provider is not gcp"
    if variables.get("MODEL_GARDEN_CLOUD_READY") != "true":
        return False, "MODEL_GARDEN_CLOUD_READY is not true"
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, "keyless GCP/OIDC configured"


def doctor(
    workspace: pathlib.Path,
    environment: str,
    repo: str | None,
    *,
    check_host: bool = False,
) -> int:
    checks: list[tuple[str, bool, str]] = []
    try:
        agents = _workspace_agents(workspace)
        checks.append(("workspace agents", True, ", ".join(agents)))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        checks.append(("workspace agents", False, str(exc)))
        agents = []

    validate = _run(["python3", str(ROOT / "scripts" / "validate-workspace.py"), str(workspace)])
    checks.append(("workspace schema", validate.returncode == 0, (validate.stdout or validate.stderr).strip()))

    try:
        lock = _load_yaml(workspace / "platform.lock.yaml", "platform.lock.yaml")
        version = lock.get("modelgarden")
        valid_version = isinstance(version, str) and bool(SEMVER.fullmatch(version))
        checks.append(("platform release pin", valid_version, str(version)))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        checks.append(("platform release pin", False, str(exc)))

    profile = None
    try:
        renderer = _load_script("render_client_environment", "render-client-environment.py")
        rendered = renderer.render(workspace / "environments" / f"{environment}.yaml", environment)
        env = rendered["spec"]
        refs = sorted(_secret_refs(rendered))
        profile = env.get("runtime", {}).get("profile") if isinstance(env.get("runtime"), dict) else None
        checks.append(("environment name", env.get("environment") == environment, str(env.get("environment"))))
        checks.append(("runtime profile", profile in agents, str(profile)))
        checks.append(("runtime secret refs", bool(refs), ", ".join(refs) if refs else "none"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        checks.append(("environment binding", False, str(exc)))

    workflow_path = workspace / ".github" / "workflows" / "model-garden.yml"
    try:
        workflow = workflow_path.read_text(encoding="utf-8")
        expected_agent = profile if isinstance(profile, str) and profile else None
        workflow_ok = bool(expected_agent and f"--agent {expected_agent}" in workflow and "deploy-client-docker.py" in workflow)
        checks.append(("generated delivery workflow", workflow_ok, expected_agent or "environment has no runtime profile"))
    except OSError as exc:
        checks.append(("generated delivery workflow", False, str(exc)))

    if repo is not None:
        try:
            _validate_repo(repo)
            _require_command("gh")
            raw_secret = _run(["gh", "secret", "list", "--env", environment, "--repo", repo])
            configured = raw_secret.returncode == 0 and "MODEL_GARDEN_RUNTIME_SECRETS_JSON" in raw_secret.stdout
            detail = "present" if configured else (raw_secret.stderr.strip() or "missing")
            checks.append(("GitHub runtime secret", configured, detail))

            env_vars = _parse_gh_variables(
                _run(["gh", "variable", "list", "--env", environment, "--repo", repo, "--json", "name,value"])
            )
            repo_vars = _parse_gh_variables(
                _run(["gh", "variable", "list", "--repo", repo, "--json", "name,value"])
            )
            runner_label = env_vars.get("MODEL_GARDEN_DOCKER_RUNNER")
            keyless_ready, keyless_detail = _keyless_gcp_ready(repo_vars)
            deployment_ready = bool(runner_label) or keyless_ready
            if runner_label:
                deployment_detail = f"self-hosted runner: {runner_label}"
            elif keyless_ready:
                deployment_detail = keyless_detail
            else:
                deployment_detail = "no self-hosted runner and keyless cloud is not ready; " + keyless_detail
            checks.append(("deployment target", deployment_ready, deployment_detail))
        except ValueError as exc:
            checks.append(("GitHub configuration", False, str(exc)))

    if check_host:
        _require_command("docker")
        ok, detail = _check_command(["docker", "version", "--format", "{{.Server.Version}}"])
        checks.append(("Docker Engine", ok, detail))
        ok, detail = _check_command(["docker", "compose", "version", "--short"])
        checks.append(("Docker Compose", ok, detail))

    failed = False
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'fail'}] {name}: {detail}")
        failed = failed or not ok
    return 1 if failed else 0


def _governed_tool_container(project_name: str) -> str:
    if not PROJECT_NAME.fullmatch(project_name):
        raise ValueError("project name must use lowercase letters, numbers, and internal hyphens")
    _require_command("docker")
    result = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
            "--filter",
            "label=com.docker.compose.service=governed-tools",
            "--format",
            "{{.ID}}",
        ]
    )
    if result.returncode != 0:
        raise ValueError(f"cannot inspect governed Tool runtime: {result.stderr.strip()}")
    containers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(containers) != 1:
        raise ValueError(
            f"expected exactly one running governed-tools container for {project_name}; found {len(containers)}"
        )
    return containers[0]


def runtime_approval(
    project_name: str,
    command: str,
    request_id: str | None = None,
    approver: str | None = None,
) -> int:
    if command not in {"list", "approve", "reject"}:
        raise ValueError("runtime approval command must be list, approve, or reject")
    container = _governed_tool_container(project_name)
    inner = [
        "python",
        "/opt/model-garden-platform/scripts/runtime-approval.py",
        "/opt/data/.modelgarden/approvals",
        command,
    ]
    if command != "list":
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("request id must be a 64-character lowercase SHA-256 hex value")
        if not isinstance(approver, str) or not approver.strip():
            raise ValueError("approver must be non-empty")
        inner.extend([request_id, "--approver", approver.strip()])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime approval command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="bootstrap a role-generic client workspace")
    init.add_argument("client_slug")
    init.add_argument("--agent", default="receptionist")
    init.add_argument("--role-title")
    init.add_argument("--output", type=pathlib.Path)
    init.add_argument("--github-repo", help="optional owner/name; creates a private repo with main and dev")

    config = sub.add_parser("configure", help="configure one GitHub dev/prod Environment securely")
    config.add_argument("workspace", type=pathlib.Path)
    config.add_argument("--repo", required=True)
    config.add_argument("--environment", choices=("dev", "prod"), required=True)
    config.add_argument(
        "--runner-label",
        help="optional legacy/self-hosted Docker runner label; omit for preferred keyless cloud deployment",
    )
    config.add_argument("--dry-run", action="store_true")

    doc = sub.add_parser("doctor", help="check workspace/deployment readiness without exposing secrets")
    doc.add_argument("workspace", type=pathlib.Path)
    doc.add_argument("--environment", choices=("dev", "prod"), default="dev")
    doc.add_argument("--repo", help="optional owner/name; verifies runtime secrets and either keyless cloud or runner readiness")
    doc.add_argument("--host", action="store_true", help="also verify local Docker Engine + Compose")

    approvals = sub.add_parser("approvals", help="list exact material actions waiting for approval")
    approvals.add_argument("--project-name", required=True)

    approve = sub.add_parser("approve", help="approve one exact material action on a client runtime")
    approve.add_argument("request_id")
    approve.add_argument("--project-name", required=True)
    approve.add_argument("--approver", required=True)

    reject = sub.add_parser("reject", help="reject one exact material action on a client runtime")
    reject.add_argument("request_id")
    reject.add_argument("--project-name", required=True)
    reject.add_argument("--approver", required=True)

    args = parser.parse_args()
    try:
        if args.command == "init":
            output = args.output or pathlib.Path(f"{args.client_slug}-ai-workspace")
            initialize(output, args.client_slug, args.agent, args.role_title, args.github_repo)
            return 0
        if args.command == "configure":
            configure(args.workspace, args.repo, args.environment, args.runner_label, dry_run=args.dry_run)
            return 0
        if args.command == "doctor":
            return doctor(args.workspace, args.environment, args.repo, check_host=args.host)
        if args.command == "approvals":
            return runtime_approval(args.project_name, "list")
        if args.command in {"approve", "reject"}:
            return runtime_approval(
                args.project_name,
                args.command,
                request_id=args.request_id,
                approver=args.approver,
            )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Client operator failed: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
