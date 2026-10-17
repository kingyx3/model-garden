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


def _service_container(project_name: str, service: str, label: str) -> str:
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
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.ID}}",
        ]
    )
    if result.returncode != 0:
        raise ValueError(f"cannot inspect {label}: {result.stderr.strip()}")
    containers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(containers) != 1:
        raise ValueError(
            f"expected exactly one running {service} container for {project_name}; found {len(containers)}"
        )
    return containers[0]


def _governed_tool_container(project_name: str) -> str:
    return _service_container(project_name, "governed-tools", "governed Tool runtime")


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


def runtime_handoff(
    project_name: str,
    command: str,
    handoff_id: str | None = None,
    by: str | None = None,
    note: str | None = None,
) -> int:
    """List Tier 4 hand-offs or close one as done/declined by a named person."""
    if command not in {"list", "done", "decline"}:
        raise ValueError("runtime hand-off command must be list, done, or decline")
    if command != "list":
        # Validate before touching Docker so bad input never reaches the runtime.
        if not isinstance(handoff_id, str) or not REQUEST_ID.fullmatch(handoff_id):
            raise ValueError("hand-off id must be a 64-character lowercase SHA-256 hex value")
        if not isinstance(by, str) or not by.strip():
            raise ValueError("--by must name the person closing the hand-off")
    container = _governed_tool_container(project_name)
    inner = [
        "python",
        "/opt/model-garden-platform/scripts/runtime-handoff.py",
        "/opt/data/.modelgarden/handoffs",
        command,
    ]
    if command != "list":
        inner.extend([handoff_id, "--by", by.strip()])
        if note:
            inner.extend(["--note", note])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime hand-off command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


MEMORY_COMMANDS = {"list", "export", "delete", "purge"}
MEMORY_ID = re.compile(r"^m-[0-9a-f]{12}$")
MEMORY_SCOPE = re.compile(r"^(client|agent:[A-Za-z0-9][A-Za-z0-9._-]{0,63}|conversation:[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$")


def runtime_memory(
    project_name: str,
    command: str,
    *,
    scopes: tuple[str, ...] = (),
    memory_id: str | None = None,
    by: str | None = None,
) -> int:
    """List, export, delete or purge what the coworker remembers; every change is ledgered with who did it."""
    if command not in MEMORY_COMMANDS:
        raise ValueError("memory command must be list, export, delete or purge")
    if any(not MEMORY_SCOPE.fullmatch(item) for item in scopes):
        raise ValueError("scope must be client, agent:<name> or conversation:<id>")
    if command == "list" and not scopes:
        raise ValueError("memory list needs at least one --scope")
    if command == "delete" and not (isinstance(memory_id, str) and MEMORY_ID.fullmatch(memory_id)):
        raise ValueError("memory id must look like m-0123456789ab")
    if command != "list" and (not isinstance(by, str) or not by.strip()):
        raise ValueError("--by must name the administrator")
    container = _governed_tool_container(project_name)
    inner = ["python", "/opt/model-garden-platform/scripts/runtime-memory.py", "/opt/data/.modelgarden/memory", command]
    if command == "list":
        for item in scopes:
            inner.extend(["--scope", item])
    else:
        if command == "delete":
            inner.append(memory_id)
        inner.extend(["--by", by.strip()])
        if command == "purge" and scopes:
            inner.extend(["--scope", scopes[0]])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime memory command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


ARTIFACT_COMMANDS = {"list", "delete", "purge-expired"}
ARTIFACT_ID = re.compile(r"^a-[0-9a-f]{12}$")
ARTIFACT_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")


def runtime_artifacts(
    project_name: str,
    command: str,
    *,
    artifact_id: str | None = None,
    by: str | None = None,
    requested_by: str | None = None,
) -> int:
    """List, delete or expire the coworker's stored deliverables; deletes and expiries are ledgered."""
    if command not in ARTIFACT_COMMANDS:
        raise ValueError("artifacts command must be list, delete or purge-expired")
    if command == "delete":
        if not (isinstance(artifact_id, str) and ARTIFACT_ID.fullmatch(artifact_id)):
            raise ValueError("artifact id must look like a-0123456789ab")
        if not isinstance(by, str) or not ARTIFACT_PERSON.fullmatch(by.strip()):
            raise ValueError("--by must name the administrator")
    if requested_by is not None and not ARTIFACT_PERSON.fullmatch(requested_by):
        raise ValueError("--requested-by must be a short identity")
    container = _governed_tool_container(project_name)
    inner = ["python", "/opt/model-garden-platform/scripts/runtime-artifacts.py", "/opt/data/.modelgarden/artifacts", command]
    if command == "list" and requested_by:
        inner.extend(["--requested-by", requested_by])
    if command == "delete":
        inner.extend([artifact_id, "--by", by.strip()])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime artifacts command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


CONVERSATION_COMMANDS = {"stats", "review", "timeline", "confirm", "dismiss", "erase", "purge"}
CONVERSATION_ID = re.compile(r"^cv-[0-9a-f]{20}$")
REVIEW_ID = re.compile(r"^rv-[0-9a-f]{16}$")
CUSTOMER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")


def runtime_conversations(
    project_name: str,
    command: str,
    *,
    target: str | None = None,
    conversation: str | None = None,
    customer: str | None = None,
    older_than_days: int | None = None,
    by: str | None = None,
) -> int:
    """Show or manage the cross-channel conversation timeline (references only); changes are ledgered under the named person."""
    if command not in CONVERSATION_COMMANDS:
        raise ValueError("conversations command must be one of " + ", ".join(sorted(CONVERSATION_COMMANDS)))
    if conversation is not None and not CONVERSATION_ID.fullmatch(conversation):
        raise ValueError("--conversation must look like cv-0123456789abcdef0123")
    if customer is not None and not CUSTOMER_ID.fullmatch(customer):
        raise ValueError("--customer must be a short customer id")
    if command in {"confirm", "dismiss"} and not (isinstance(target, str) and REVIEW_ID.fullmatch(target)):
        raise ValueError("a review id like rv-0123456789abcdef is required")
    if command in {"confirm", "dismiss", "erase", "purge"} and not (isinstance(by, str) and ARTIFACT_PERSON.fullmatch(by.strip())):
        raise ValueError("--by must name the person deciding")
    if command == "timeline" and (conversation is None) == (customer is None):
        raise ValueError("timeline needs exactly one of --conversation or --customer")
    if command == "erase" and (conversation is None) == (customer is None):
        raise ValueError("erase needs exactly one of --conversation or --customer")
    if command == "confirm" and customer is None:
        raise ValueError("confirm needs --customer")
    if command == "purge" and not (isinstance(older_than_days, int) and 1 <= older_than_days <= 3650):
        raise ValueError("--older-than-days is between 1 and 3650")
    container = _governed_tool_container(project_name)
    inner = ["python", "/opt/model-garden-platform/scripts/runtime-conversations.py", "/opt/data/.modelgarden/conversations", command]
    if command in {"confirm", "dismiss"}:
        inner.append(target)
    if conversation:
        inner.extend(["--conversation", conversation])
    if customer:
        inner.extend(["--customer", customer])
    if command == "purge":
        inner.extend(["--older-than-days", str(older_than_days)])
    if command in {"confirm", "dismiss", "erase", "purge"}:
        inner.extend(["--by", by.strip()])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime conversations command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


NOTICE_KINDS = ("assigned", "reminder", "escalation")
CHANNEL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def runtime_handoff_notices(
    project_name: str,
    command: str,
    *,
    handoff_id: str | None = None,
    kind: str | None = None,
    via: str | None = None,
    escalate_to: str | None = None,
) -> int:
    """Print the hand-off notices that should reach a person, or record that one was delivered."""
    if command not in {"due", "notified"}:
        raise ValueError("hand-off notice command must be due or notified")
    if command == "notified":
        if not isinstance(handoff_id, str) or not REQUEST_ID.fullmatch(handoff_id):
            raise ValueError("hand-off id must be a 64-character lowercase SHA-256 hex value")
        if kind not in NOTICE_KINDS:
            raise ValueError(f"notice kind must be one of {', '.join(NOTICE_KINDS)}")
        if not isinstance(via, str) or not CHANNEL_NAME.fullmatch(via):
            raise ValueError("--via must name the delivering channel, for example email or slack")
    container = _governed_tool_container(project_name)
    inner = ["python", "/opt/model-garden-platform/scripts/runtime-handoff.py", "/opt/data/.modelgarden/handoffs", command]
    if command == "notified":
        inner.extend([handoff_id, kind, "--via", via])
    elif escalate_to:
        inner.extend(["--escalate-to", escalate_to])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime hand-off notice command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


CONTACT_COMMANDS = {"review", "assign", "dismiss", "erase-contact", "erase-interaction"}
CONTACT_ID = re.compile(r"^(?:c|i|r)-[0-9a-f]{12,24}$")


def runtime_contacts(
    project_name: str,
    command: str,
    ids: tuple[str, ...] = (),
    by: str | None = None,
) -> int:
    """Review or erase records in the Tier 0 contact log; every change is ledgered with who did it."""
    if command not in CONTACT_COMMANDS:
        raise ValueError("contacts command must be review, assign, dismiss, erase-contact or erase-interaction")
    expected = {"review": 0, "assign": 2, "dismiss": 1, "erase-contact": 1, "erase-interaction": 1}[command]
    if len(ids) != expected:
        raise ValueError(f"contacts {command} needs {expected} identifier(s)")
    if any(not CONTACT_ID.fullmatch(item) and item != "unmatched" for item in ids):
        raise ValueError("identifiers must be record IDs such as c-..., i-... or r-...")
    if command != "review" and (not isinstance(by, str) or not by.strip()):
        raise ValueError("--by must name the person performing the action")
    container = _governed_tool_container(project_name)
    inner = ["python", "/opt/model-garden-platform/scripts/runtime-contacts.py", "/opt/data/.modelgarden/contacts", command, *ids]
    if command != "review":
        inner.extend(["--by", by.strip()])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime contacts command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


VOICE_TRANSCRIPTS = "/opt/data/transcripts.jsonl"
VOICE_MESSAGES = "/opt/data/messages.jsonl"  # lifecycle events, including TranscriptWriteFailed


def runtime_file_calls(project_name: str) -> int:
    """File the voice worker's stored call transcripts into the bound CRM (ledgered, idempotent)."""
    voice = _service_container(project_name, "voice", "voice worker")
    container = _governed_tool_container(project_name)
    stored = _run(["docker", "exec", voice, "sh", "-c", f"cat {VOICE_TRANSCRIPTS} {VOICE_MESSAGES} 2>/dev/null || true"])
    if stored.returncode != 0:
        raise ValueError(stored.stderr.strip() or "cannot read the voice worker's transcripts")
    result = _run(
        ["docker", "exec", "-i", container, "python", "/opt/model-garden-platform/scripts/runtime-file-calls.py", "--transcripts", "-"],
        input_text=stored.stdout,
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "runtime file-calls command failed")
    return 0


def runtime_inbox(project_name: str, as_json: bool = False) -> int:
    """Show everything waiting for a person: approvals, hand-offs and records to review (no personal data)."""
    container = _governed_tool_container(project_name)
    inner = ["python", "/opt/model-garden-platform/scripts/runtime-inbox.py"]
    if as_json:
        inner.append("--json")
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime inbox command failed")
    if result.stdout:
        print(result.stdout.rstrip())
    return 0


def runtime_connection(
    project_name: str,
    command: str,
    family: str | None = None,
    health: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
    agent: str | None = None,
) -> int:
    """Show or set live connection health; the capability manifest reflects it immediately."""
    if command not in {"list", "set", "offboard"}:
        raise ValueError("runtime connection command must be list, set or offboard")
    if command == "offboard" and (not isinstance(actor, str) or not actor.strip()):
        raise ValueError("--actor must name the person offboarding")
    if agent is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", agent):
        raise ValueError("--agent must be a short Agent name")
    container = _governed_tool_container(project_name)
    inner = [
        "python",
        "/opt/model-garden-platform/scripts/runtime-connection.py",
        "/opt/data/.modelgarden",
        command,
    ]
    if command == "set":
        if not isinstance(family, str) or not re.fullmatch(r"[a-z]+", family):
            raise ValueError("connection family must be a lowercase capability family, e.g. calendar")
        if health not in {"healthy", "degraded", "failed", "revoked"}:
            raise ValueError("connection health must be healthy, degraded, failed or revoked")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be non-empty")
        inner.extend([family, health, "--actor", actor.strip()])
        if reason:
            inner.extend(["--reason", reason])
    if command == "offboard":
        inner.extend(["--actor", actor.strip()])
        if agent:
            inner.extend(["--agent", agent])
        if reason:
            inner.extend(["--reason", reason])
    result = _run(["docker", "exec", container, *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime connection command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


def runtime_grants(project_name: str, command: str, *, agent: str | None = None, grant_id: str | None = None, actor: str | None = None) -> int:
    """Show or revoke per-coworker provider grants on a client runtime (no secret reference is shown)."""
    if command not in {"list", "revoke"}:
        raise ValueError("grants command must be list or revoke")
    inner = ["python", "/opt/model-garden-platform/scripts/runtime-connection.py", "/opt/data/.modelgarden", "grants", command]
    if command == "list":
        if agent is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", agent):
                raise ValueError("--agent must be a short Agent name")
            inner.extend(["--agent", agent])
    else:
        if not isinstance(grant_id, str) or not re.fullmatch(r"g-[0-9a-f]{12}", grant_id):
            raise ValueError("grant id must look like g-0123456789ab")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("--actor must name the person revoking the grant")
        inner.extend([grant_id, "--actor", actor.strip()])
    result = _run(["docker", "exec", _governed_tool_container(project_name), *inner])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "runtime grants command failed")
    if result.stdout:
        print(result.stdout.strip())
    return 0


def _audit_module():
    path = ROOT / "platform" / "core" / "audit_query.py"
    name = "modelgarden_audit_query"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def runtime_audit(args: argparse.Namespace) -> int:
    """Ask the Action Ledger: filtered query, task timeline, policy replay, sandbox dry-run, CSV/JSON export (read-only)."""
    audit = _audit_module()
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        if args.ledger is not None:
            ledger_path = args.ledger
        else:
            if not args.project_name:
                raise ValueError("give --ledger (a ledger or export file) or --project-name (read it from the running client)")
            container = _governed_tool_container(args.project_name)
            fetched = _run(["docker", "exec", container, "cat", "/opt/data/.modelgarden/audit.jsonl"])
            if fetched.returncode != 0:
                raise ValueError(fetched.stderr.strip() or "cannot read the ledger from the client runtime")
            ledger_path = pathlib.Path(scratch) / "audit.jsonl"
            ledger_path.write_text(fetched.stdout, encoding="utf-8")
        verdict = audit.verify(ledger_path)
        if not verdict["valid"] and not args.allow_unverified:
            raise ValueError(f"the ledger chain does not verify ({verdict.get('error')}); fix that first or pass --allow-unverified to look anyway")
        events = audit.read_ledger(ledger_path)
        criteria = audit.Filter(
            agent=args.agent, principal=args.principal, approver=args.approver, capability=args.capability, provider=args.provider,
            tool=args.tool, event=args.event_type, outcome=args.outcome, since=args.since, until=args.until, task=args.task,
            correlation=args.correlation, external_object=args.external_object, request=args.request,
        )
        scoped = audit.query(events, criteria)
        mode, columns = args.audit_command, audit.EVENT_COLUMNS
        if mode == "query":
            document: Any = {"chain": {k: verdict.get(k) for k in ("valid", "events", "headHash")}, "matched": len(scoped), "events": scoped}
            rows = audit.event_rows(scoped)
        elif mode == "timeline":
            document = audit.timeline(events, task=args.task, correlation=args.correlation)
            rows = [{key: step.get(key) for key in ("sequence", "timestamp", "event", "outcome", "agent", "tool", "requestId")} for step in document["steps"]]
            columns = ("sequence", "timestamp", "event", "outcome", "agent", "tool", "requestId")
        elif mode in ("replay-policy", "replay-dry-run"):
            if args.desired_state is None and mode == "replay-policy":
                raise ValueError("replay-policy needs --desired-state (the current or a historical compiled desired state)")
            if mode == "replay-policy":
                desired = json.loads(args.desired_state.read_text(encoding="utf-8"))
                document = audit.replay_policy(scoped, desired)
                rows = [{"requestId": d["requestId"], "agent": d["agent"], "tool": d["tool"], "recorded": d["recorded"]["decision"],
                         "replayed": d["replayed"]["decision"], "inputsIncomplete": d["inputsIncomplete"]} for d in document["differences"]]
                columns = ("requestId", "agent", "tool", "recorded", "replayed", "inputsIncomplete")
            else:
                document = audit.dry_run_replay(scoped, {}, default=audit.MockConnector())
                rows = [{key: step.get(key) for key in ("requestId", "sequence", "tool", "recorded", "status")} for step in document["steps"]]
                columns = ("requestId", "sequence", "tool", "recorded", "status")
        else:
            raise ValueError("unknown audit command")
    text = audit.to_csv(rows, columns) if args.format == "csv" else audit.to_json(document)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(text)
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

    handoffs = sub.add_parser("handoffs", help="list actions the coworker prepared for a named person (Tier 4 hand-offs)")
    handoffs.add_argument("--project-name", required=True)

    for outcome in ("handoff-done", "handoff-decline"):
        closer = sub.add_parser(outcome, help=f"record that a named person {outcome.split('-')[1]} a hand-off")
        closer.add_argument("handoff_id")
        closer.add_argument("--project-name", required=True)
        closer.add_argument("--by", required=True, help="the named person who did or declined it")
        closer.add_argument("--note")

    due = sub.add_parser("handoffs-due", help="print hand-off notices that should reach a person now (no action arguments)")
    due.add_argument("--project-name", required=True)
    due.add_argument("--escalate-to", help="who an overdue hand-off is escalated to (default: the assignee again)")
    notified = sub.add_parser("handoff-notified", help="record that a hand-off notice was delivered through a channel")
    notified.add_argument("handoff_id")
    notified.add_argument("kind", choices=NOTICE_KINDS)
    notified.add_argument("--project-name", required=True)
    notified.add_argument("--via", required=True)
    memory = sub.add_parser("memory", help="inspect, export, delete or purge what the coworker remembers (client data; run on the client host)")
    memory.add_argument("action", choices=sorted(MEMORY_COMMANDS))
    memory.add_argument("memory_id", nargs="?", help="entry to delete (m-...)")
    memory.add_argument("--project-name", required=True)
    memory.add_argument("--scope", action="append", default=[], help="client, agent:<name> or conversation:<id>")
    memory.add_argument("--by", help="the named administrator for export, delete and purge")
    artifacts = sub.add_parser("artifacts", help="list, delete or expire the coworker's stored deliverables (client data; run on the client host)")
    artifacts.add_argument("action", choices=sorted(ARTIFACT_COMMANDS))
    artifacts.add_argument("artifact_id", nargs="?", help="deliverable to delete (a-...)")
    artifacts.add_argument("--project-name", required=True)
    artifacts.add_argument("--by", help="the named administrator for delete")
    artifacts.add_argument("--requested-by", help="list only what this person asked for")
    conversations = sub.add_parser(
        "conversations", help="show or manage the cross-channel conversation timeline: review queue, timelines, erasure (references only; run on the client host)"
    )
    conversations.add_argument("action", choices=sorted(CONVERSATION_COMMANDS))
    conversations.add_argument("target", nargs="?", help="the review id (rv-...) for confirm or dismiss")
    conversations.add_argument("--project-name", required=True)
    conversations.add_argument("--conversation", help="a conversation id (cv-...)")
    conversations.add_argument("--customer", help="a customer id")
    conversations.add_argument("--older-than-days", type=int, help="retention: remove conversations older than this")
    conversations.add_argument("--by", help="the named person deciding, for confirm, dismiss, erase and purge")
    inbox = sub.add_parser("inbox", help="show everything waiting for a person: approvals, hand-offs, records to review")
    inbox.add_argument("--project-name", required=True)
    inbox.add_argument("--json", action="store_true")
    file_calls = sub.add_parser(
        "file-calls", help="file the voice worker's stored call transcripts into the bound CRM (unmatched queue; run on the client host)"
    )
    file_calls.add_argument("--project-name", required=True)
    contacts = sub.add_parser("contacts", help="review or erase the Tier 0 contact log (personal data; run on the client host)")
    contacts.add_argument("action", choices=sorted(CONTACT_COMMANDS))
    contacts.add_argument("ids", nargs="*", help="record IDs the action applies to")
    contacts.add_argument("--project-name", required=True)
    contacts.add_argument("--by", help="the named person performing a change")

    skills = sub.add_parser("skills", help="show enabled, disabled and blocked Skills for a client environment")
    skills.add_argument("workspace", type=pathlib.Path, help="private client workspace")
    skills.add_argument("--environment", choices=("dev", "prod"), default="dev")
    skills.add_argument("--agent", help="optional named Agent")

    change = sub.add_parser("readiness-change", help="preview which Skills a connection would unblock or break (writes nothing)")
    change.add_argument("workspace", type=pathlib.Path, help="private client workspace")
    change.add_argument("family", help="capability family, e.g. crm")
    change.add_argument("--provider", required=True, help="the provider id the connection would bind")
    change.add_argument("--tier", type=int, required=True, help="that provider's tier")
    change.add_argument("--scopes", help="comma-separated scopes the connection would hold")
    change.add_argument("--owner", help="the named owner of the connection")
    change.add_argument("--replace", action="store_true", help="allow the provider to replace an existing one")
    change.add_argument("--rescope", action="store_true", help="allow widening scopes or changing owner of an existing binding")
    change.add_argument("--environment", choices=("dev", "prod"), default="dev")
    change.add_argument("--agent", help="optional named Agent")
    change.add_argument("--json", action="store_true", help="print the comparison as JSON")

    pack = sub.add_parser("onboarding-pack", help="write the owner-readable pack: what works, what needs one thing from you, what runs without asking")
    pack.add_argument("workspace", type=pathlib.Path, help="private client workspace")
    pack.add_argument("--environment", choices=("dev", "prod"), default="dev")
    pack.add_argument("--agent", help="optional named Agent")
    pack.add_argument("--output", type=pathlib.Path, help="write Markdown here instead of printing it")

    connect = sub.add_parser("connect", help="list provider options or preview exact requested permissions (no credentials handled)")
    connect.add_argument("family", help="portable capability family, e.g. calendar, crm, kb, mail")
    connect.add_argument("--provider", help="registered provider ID; omit to list all provider choices")
    connect.add_argument("--tools", nargs="+", help="exact portable Tools approved by the client owner")
    connect.add_argument("--owner", help="named client owner authorizing the connection (non-secret)")
    connect.add_argument("--operator", help="named Model Garden operator (non-secret)")

    connections = sub.add_parser("connections", help="show live connector health on a client runtime")
    connections.add_argument("--project-name", required=True)

    offboard = sub.add_parser(
        "offboard", help="revoke every bound external connection and print the manual provider, secret and data steps"
    )
    offboard.add_argument("--project-name", required=True)
    offboard.add_argument("--actor", required=True, help="the named person offboarding")
    offboard.add_argument("--reason")
    offboard.add_argument("--agent", help="offboard this coworker only (revoke its provider grants; every other coworker keeps working)")
    grants = sub.add_parser("grants", help="show or revoke per-coworker provider grants on a client runtime")
    grants.add_argument("grants_command", choices=("list", "revoke"))
    grants.add_argument("grant_id", nargs="?", help="grant to revoke (g-...)")
    grants.add_argument("--project-name", required=True)
    grants.add_argument("--agent", help="list only this coworker's grants")
    grants.add_argument("--actor", help="the named person revoking (required for revoke)")
    connection = sub.add_parser("connection", help="mark one connector healthy, degraded, failed or revoked")
    connection.add_argument("family", help="capability family, e.g. calendar")
    connection.add_argument("health", choices=("healthy", "degraded", "failed", "revoked"))
    connection.add_argument("--project-name", required=True)
    connection.add_argument("--actor", required=True)
    connection.add_argument("--reason")

    audit = sub.add_parser(
        "audit",
        help="query the Action Ledger: filter, task timeline, policy replay, sandbox dry-run, CSV/JSON export (read-only)",
    )
    audit.add_argument("audit_command", choices=("query", "timeline", "replay-policy", "replay-dry-run"))
    audit.add_argument("--ledger", type=pathlib.Path, help="a ledger or export file (otherwise read from --project-name)")
    audit.add_argument("--project-name")
    audit.add_argument("--desired-state", type=pathlib.Path, help="compiled desired state to replay policy against (current or historical)")
    audit.add_argument("--format", choices=("json", "csv"), default="json")
    audit.add_argument("--output", type=pathlib.Path)
    audit.add_argument("--allow-unverified", action="store_true", help="look at a ledger whose chain does not verify")
    for flag in ("agent", "principal", "approver", "capability", "provider", "tool", "outcome", "since", "until", "task", "correlation",
                 "external-object", "request"):
        audit.add_argument(f"--{flag}")
    audit.add_argument("--event-type", help="authorization, approval, execution, read, handoff, policy-change, operator-action")

    args = parser.parse_args()
    try:
        if args.command == "skills":
            compiler = _load_script("modelgarden_client_skills", "compile-workspace.py")
            binding_path = args.workspace / "environments" / f"{args.environment}.yaml"
            binding = compiler.load_environment_binding(binding_path)
            report = compiler.workspace_readiness(args.workspace, args.agent, (), binding)
            sys.stdout.write(compiler.format_readiness(report))
            return 0 if all(
                skill["ready"] or not skill.get("enabled", True)
                for agent in report for skill in agent["skills"]
            ) else 1
        if args.command == "readiness-change":
            compiler = _load_script("modelgarden_client_change", "compile-workspace.py")
            change_module = _load_script("modelgarden_readiness_change", "../platform/core/readiness_change.py")
            connect_module = _load_script("modelgarden_connect_for_change", "../platform/core/connect.py")
            binding = compiler.load_environment_binding(args.workspace / "environments" / f"{args.environment}.yaml")
            fragment = {"provider": args.provider, "tier": args.tier}
            if args.scopes:
                fragment["scopes"] = sorted({item.strip() for item in args.scopes.split(",") if item.strip()})
            if args.owner:
                fragment["owner"] = args.owner
            merged = connect_module.merge_binding(binding, args.family, fragment, replace=args.replace, rescope=args.rescope)
            before = compiler.workspace_readiness(args.workspace, args.agent, (), binding)
            after = compiler.workspace_readiness(args.workspace, args.agent, (), merged)
            changes = change_module.diff(before, after)
            if args.json:
                print(json.dumps(changes, indent=2, sort_keys=True))
            else:
                sys.stdout.write(change_module.render(changes, subject=f"binding {args.family} to {args.provider}"))
                print("Nothing was written: this is a preview. The binding changes only through the guided connect step.")
            return 1 if any(item["regressed"] for item in changes) else 0
        if args.command == "onboarding-pack":
            compiler = _load_script("modelgarden_client_pack", "compile-workspace.py")
            pack_module = _load_script("modelgarden_onboarding_pack", "../platform/core/onboarding_pack.py")
            registry = _load_script("modelgarden_pack_registry", "integration-directory.py").integrations
            binding = compiler.load_environment_binding(args.workspace / "environments" / f"{args.environment}.yaml")
            text = pack_module.render(pack_module.build(
                compiler.workspace_readiness(args.workspace, args.agent, (), binding),
                compiler.workspace_approvals(args.workspace, args.agent, ()),
                options=registry.options,
            ))
            if args.output:
                args.output.write_text(text, encoding="utf-8")
                print(f"Onboarding pack written to {args.output}")
            else:
                sys.stdout.write(text)
            return 0
        if args.command == "connect":
            integration_module = _load_script("modelgarden_integration_registry", "integration-directory.py")
            registry = integration_module.integrations
            providers = registry.options(args.family)
            if not args.provider:
                for item in providers:
                    print(f"{item['id']:<25} tier {item['tier']} {item['status']:<24} {item['method']:<14} #{item['issue']}")
                return 0
            if not args.owner or not args.operator:
                raise ValueError("connection plan requires --owner and --operator identities (no passwords or tokens)")
            plan = registry.plan(args.family, args.provider, args.tools or [])
            plan["clientOwner"] = args.owner
            plan["operator"] = args.operator
            print(json.dumps(plan, sort_keys=True, indent=2))
            return 0 if plan["status"] in registry.BUILT else 1
        if args.command == "connections":
            return runtime_connection(args.project_name, "list")
        if args.command == "offboard":
            return runtime_connection(args.project_name, "offboard", actor=args.actor, reason=args.reason, agent=args.agent)
        if args.command == "audit":
            return runtime_audit(args)
        if args.command == "grants":
            return runtime_grants(args.project_name, args.grants_command, agent=args.agent, grant_id=args.grant_id, actor=args.actor)
        if args.command == "connection":
            return runtime_connection(
                args.project_name, "set", family=args.family, health=args.health, actor=args.actor, reason=args.reason
            )
        if args.command == "init":
            output = args.output or pathlib.Path(f"{args.client_slug}-ai-workspace")
            initialize(output, args.client_slug, args.agent, args.role_title, args.github_repo)
            return 0
        if args.command == "configure":
            configure(args.workspace, args.repo, args.environment, args.runner_label, dry_run=args.dry_run)
            return 0
        if args.command == "doctor":
            return doctor(args.workspace, args.environment, args.repo, check_host=args.host)
        if args.command == "handoffs-due":
            return runtime_handoff_notices(args.project_name, "due", escalate_to=args.escalate_to)
        if args.command == "handoff-notified":
            return runtime_handoff_notices(
                args.project_name, "notified", handoff_id=args.handoff_id, kind=args.kind, via=args.via
            )
        if args.command == "memory":
            return runtime_memory(args.project_name, args.action, scopes=tuple(args.scope), memory_id=args.memory_id, by=args.by)
        if args.command == "conversations":
            return runtime_conversations(
                args.project_name, args.action, target=args.target, conversation=args.conversation, customer=args.customer,
                older_than_days=args.older_than_days, by=args.by,
            )
        if args.command == "artifacts":
            return runtime_artifacts(args.project_name, args.action, artifact_id=args.artifact_id, by=args.by, requested_by=args.requested_by)
        if args.command == "inbox":
            return runtime_inbox(args.project_name, as_json=args.json)
        if args.command == "file-calls":
            return runtime_file_calls(args.project_name)
        if args.command == "contacts":
            return runtime_contacts(args.project_name, args.action, tuple(args.ids), by=args.by)
        if args.command == "handoffs":
            return runtime_handoff(args.project_name, "list")
        if args.command in {"handoff-done", "handoff-decline"}:
            return runtime_handoff(
                args.project_name,
                "done" if args.command == "handoff-done" else "decline",
                handoff_id=args.handoff_id,
                by=args.by,
                note=args.note,
            )
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
