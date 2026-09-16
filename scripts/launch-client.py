#!/usr/bin/env python3
"""Bootstrap a Model Garden client from zero to verified keyless DEV in one command.

This is the preferred operator entry point for early managed clients. It composes the
existing versioned workspace, GitHub Environment and GCP bootstrap contracts instead of
creating a new control plane.

The command is deliberately resumable. On a rerun it reuses an existing private client
repository, existing environment runtime secrets, completed keyless cloud bootstrap and
a previously verified DEV deployment instead of asking the operator to repeat them.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLATFORM_REPO = "kingyx3/model-garden"
CLOUD_WORKFLOW = "Model Garden keyless GCP deployment"
VERIFIED_VARIABLE = "MODEL_GARDEN_BOOTSTRAP_VERIFIED"
GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Run = Callable[..., subprocess.CompletedProcess[str]]


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
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "input": input_text,
        "text": True,
        "check": False,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return subprocess.run(command, **kwargs)


def _checked(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        if not detail and isinstance(result.stdout, str):
            detail = result.stdout.strip()
        raise ValueError(f"{label} failed: {detail or 'command failed'}")
    return result.stdout if isinstance(result.stdout, str) else ""


def _require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise ValueError(f"required command is not installed or not on PATH: {name}")


def _repo_exists(repo: str, runner: Run) -> bool:
    return runner(["gh", "repo", "view", repo, "--json", "nameWithOwner"]).returncode == 0


def _require_private_repo(repo: str, runner: Run) -> None:
    result = runner(["gh", "repo", "view", repo, "--json", "nameWithOwner,isPrivate"])
    raw = _checked(result, "inspect client GitHub repository")
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("cannot parse client GitHub repository metadata") from exc
    actual = payload.get("nameWithOwner")
    if not isinstance(actual, str) or actual.lower() != repo.lower():
        raise ValueError(f"requested GitHub repository {repo!r} resolved to unexpected repository {actual!r}")
    if payload.get("isPrivate") is not True:
        raise ValueError("client workspace repository must be private before runtime secrets or cloud deployment are configured")


def _workspace_exists(workspace: pathlib.Path) -> bool:
    return (workspace / "modelgarden.yaml").is_file() and (workspace / "platform.lock.yaml").is_file()


def _workspace_client(workspace: pathlib.Path) -> str:
    try:
        document = yaml.safe_load((workspace / "modelgarden.yaml").read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("cannot parse client workspace modelgarden.yaml") from exc
    config = document.get("workspace") if isinstance(document, dict) else None
    client = config.get("client") if isinstance(config, dict) else None
    if not isinstance(client, str) or not client:
        raise ValueError("client workspace modelgarden.yaml is missing workspace.client")
    return client


def _github_repo_from_remote(remote: str) -> str:
    value = remote.strip()
    if value.startswith("git@github.com:"):
        candidate = value[len("git@github.com:") :]
    else:
        prefixes = (
            "https://github.com/",
            "http://github.com/",
            "ssh://git@github.com/",
        )
        prefix = next((item for item in prefixes if value.startswith(item)), None)
        if prefix is None:
            raise ValueError("client workspace origin must be a github.com owner/name repository")
        candidate = value[len(prefix) :]
    candidate = candidate.rstrip("/")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    if not GITHUB_REPO.fullmatch(candidate):
        raise ValueError("client workspace origin must resolve to github.com owner/name")
    return candidate


def _validate_workspace_identity(
    workspace: pathlib.Path,
    client_slug: str,
    repo: str,
    runner: Run,
) -> None:
    client = _workspace_client(workspace)
    if client != client_slug:
        raise ValueError(
            f"local workspace belongs to client {client!r}, not requested client {client_slug!r}; refusing cross-client launch"
        )
    result = runner(["git", "remote", "get-url", "origin"], cwd=workspace)
    origin = _checked(result, "inspect client workspace origin").strip()
    actual_repo = _github_repo_from_remote(origin)
    if actual_repo.lower() != repo.lower():
        raise ValueError(
            f"local workspace origin is {actual_repo!r}, not requested repository {repo!r}; refusing cross-repository launch"
        )


def _parse_variables(raw: str) -> dict[str, str]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("cannot parse GitHub repository variables") from exc
    if not isinstance(payload, list):
        raise ValueError("GitHub repository variables returned an unexpected response shape")
    result: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
            raise ValueError("GitHub repository variables returned an invalid item")
        result[item["name"]] = item["value"]
    return result


def _repo_variables(repo: str, runner: Run) -> dict[str, str]:
    result = runner(["gh", "variable", "list", "--repo", repo, "--json", "name,value"])
    raw = _checked(result, "read GitHub repository variables")
    return _parse_variables(raw)


def _runtime_secret_present(repo: str, environment: str, runner: Run) -> bool:
    result = runner(["gh", "secret", "list", "--env", environment, "--repo", repo])
    return result.returncode == 0 and "MODEL_GARDEN_RUNTIME_SECRETS_JSON" in (result.stdout or "")


def _cloud_ready(variables: dict[str, str]) -> bool:
    required = {
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
    return (
        variables.get("MODEL_GARDEN_CLOUD_PROVIDER") == "gcp"
        and variables.get("MODEL_GARDEN_CLOUD_READY") == "true"
        and all(variables.get(name) for name in required)
    )


def _cloud_mismatches(
    variables: dict[str, str],
    *,
    project_id: str,
    region: str,
    zone: str,
    ownership: str,
) -> list[str]:
    expected = {
        "GCP_PROJECT_ID": project_id,
        "GCP_REGION": region,
        "GCP_ZONE": zone,
        "MODEL_GARDEN_INFRA_OWNERSHIP": ownership,
    }
    return [
        f"{name}={variables.get(name)!r} (requested {value!r})"
        for name, value in expected.items()
        if variables.get(name) != value
    ]


def _ensure_release_exists(runner: Run) -> str:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tag = f"v{version}"
    result = runner(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            PLATFORM_REPO,
            "--json",
            "tagName,isDraft,isPrerelease",
        ]
    )
    if result.returncode != 0:
        raise ValueError(
            f"Model Garden {tag} is not published yet; client bootstrap must use a released platform version"
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot parse release metadata for {tag}") from exc
    if payload.get("tagName") != tag or payload.get("isDraft") or payload.get("isPrerelease"):
        raise ValueError(f"Model Garden {tag} is not a published production release")
    return version


def _run_operator(
    args: list[str],
    *,
    runner: Run,
    capture: bool = False,
) -> None:
    result = runner([sys.executable, str(ROOT / "scripts" / "client-operator.py"), *args], capture=capture)
    _checked(result, "client operator")


def _run_cloud_bootstrap(
    *,
    project_id: str,
    repo: str,
    client_slug: str,
    agent_slug: str,
    credential_file: pathlib.Path,
    region: str,
    zone: str,
    ownership: str,
    runner: Run,
) -> None:
    result = runner(
        [
            sys.executable,
            str(ROOT / "scripts" / "bootstrap-cloud.py"),
            "gcp",
            "--project",
            project_id,
            "--repo",
            repo,
            "--client-slug",
            client_slug,
            "--agent",
            agent_slug,
            "--credential-file",
            str(credential_file),
            "--region",
            region,
            "--zone",
            zone,
            "--ownership",
            ownership,
        ],
        capture=False,
    )
    _checked(result, "keyless cloud bootstrap")


def _ensure_workspace_and_repo(
    *,
    workspace: pathlib.Path,
    client_slug: str,
    agent_slug: str,
    role_title: str | None,
    repo: str,
    runner: Run,
) -> None:
    workspace_exists = _workspace_exists(workspace)
    repo_exists = _repo_exists(repo, runner)

    if repo_exists:
        _require_private_repo(repo, runner)
    if workspace_exists and repo_exists:
        _validate_workspace_identity(workspace, client_slug, repo, runner)
        return
    if not workspace_exists and repo_exists:
        _checked(runner(["gh", "repo", "clone", repo, str(workspace)]), "clone existing client workspace")
        if not _workspace_exists(workspace):
            raise ValueError("cloned client repository is not a Model Garden workspace")
        _validate_workspace_identity(workspace, client_slug, repo, runner)
        return
    if workspace_exists and not repo_exists:
        raise ValueError(
            "client workspace already exists locally but the requested GitHub repository does not; "
            "use a new empty workspace path or create/push the repository deliberately"
        )

    command = [
        "init",
        client_slug,
        "--agent",
        agent_slug,
        "--output",
        str(workspace),
        "--github-repo",
        repo,
    ]
    if role_title:
        command.extend(["--role-title", role_title])
    _run_operator(command, runner=runner)
    _validate_workspace_identity(workspace, client_slug, repo, runner)


def _ensure_runtime_secrets(
    workspace: pathlib.Path,
    repo: str,
    *,
    runner: Run,
    rotate: bool,
) -> None:
    for environment in ("dev", "prod"):
        if not rotate and _runtime_secret_present(repo, environment, runner):
            print(f"Reusing existing {environment} runtime secret map.")
            continue
        _run_operator(
            ["configure", str(workspace), "--repo", repo, "--environment", environment],
            runner=runner,
        )


def _activate_dev(workspace: pathlib.Path, runner: Run) -> None:
    commands = [
        ["git", "checkout", "dev"],
        ["git", "commit", "--allow-empty", "-m", "Activate Model Garden keyless DEV deployment"],
        ["git", "push", "origin", "dev"],
        ["git", "checkout", "main"],
        ["git", "pull", "--ff-only", "origin", "main"],
    ]
    for command in commands:
        _checked(runner(command, cwd=workspace), " ".join(command[:2]))


def _run_ids(repo: str, runner: Run) -> set[int]:
    result = runner(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            CLOUD_WORKFLOW,
            "--limit",
            "20",
            "--json",
            "databaseId",
        ]
    )
    raw = _checked(result, "list keyless GCP deployment runs")
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("cannot parse keyless GCP deployment runs") from exc
    if not isinstance(payload, list):
        raise ValueError("keyless GCP deployment runs returned an unexpected response shape")
    run_ids: set[int] = set()
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("databaseId"), int):
            raise ValueError("keyless GCP deployment runs returned an invalid item")
        run_ids.add(int(item["databaseId"]))
    return run_ids


def _wait_for_new_cloud_run(
    repo: str,
    previous_ids: set[int],
    *,
    runner: Run,
    poll_seconds: float = 5.0,
    attempts: int = 120,
) -> int:
    for _ in range(attempts):
        current = _run_ids(repo, runner)
        new_ids = sorted(current - previous_ids, reverse=True)
        if new_ids:
            run_id = new_ids[0]
            result = runner(["gh", "run", "watch", str(run_id), "--repo", repo, "--exit-status"], capture=False)
            _checked(result, f"keyless DEV deployment run {run_id}")
            return run_id
        if poll_seconds:
            time.sleep(poll_seconds)
    raise ValueError("timed out waiting for the keyless GCP deployment workflow to start")


def _set_verified(repo: str, runner: Run) -> None:
    _checked(
        runner(["gh", "variable", "set", VERIFIED_VARIABLE, "--repo", repo, "--body", "true"]),
        "record verified bootstrap",
    )


def _revoke_bootstrap_key(
    credential_file: pathlib.Path,
    *,
    runner: Run,
    delete_file: bool,
) -> bool:
    cloud = _load_script("bootstrap_cloud_for_revoke", "bootstrap-cloud.py")
    key = cloud.load_service_account_key(credential_file)
    if shutil.which("gcloud") is None:
        print("Bootstrap is keyless and verified, but gcloud is not installed locally.", file=sys.stderr)
        print(
            "Revoke the temporary key once with: gcloud iam service-accounts keys delete "
            f"{key['private_key_id']} --iam-account {key['client_email']} --project {key['project_id']}",
            file=sys.stderr,
        )
        print(f"Then delete {credential_file}.", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory(prefix="model-garden-gcloud-") as config:
        env = os.environ.copy()
        env["CLOUDSDK_CONFIG"] = config
        _checked(
            runner(
                ["gcloud", "auth", "activate-service-account", key["client_email"], "--key-file", str(credential_file), "--quiet"],
                env=env,
            ),
            "activate isolated bootstrap credential for revocation",
        )
        _checked(
            runner(
                [
                    "gcloud",
                    "iam",
                    "service-accounts",
                    "keys",
                    "delete",
                    key["private_key_id"],
                    "--iam-account",
                    key["client_email"],
                    "--project",
                    key["project_id"],
                    "--quiet",
                ],
                env=env,
            ),
            "revoke temporary bootstrap key",
        )
    if delete_file:
        credential_file.unlink()
        print(f"Revoked bootstrap key and deleted local credential file {credential_file}.")
    else:
        print("Revoked bootstrap key. Local credential file retained only because --keep-bootstrap-credential was supplied.")
    return True


def launch(
    *,
    client_slug: str,
    github_repo: str,
    project_id: str,
    credential_file: pathlib.Path,
    workspace: pathlib.Path,
    agent_slug: str = "receptionist",
    role_title: str | None = None,
    region: str = "asia-southeast1",
    zone: str = "asia-southeast1-b",
    ownership: str = "model-garden",
    rotate_runtime_secrets: bool = False,
    keep_bootstrap_credential: bool = False,
    no_wait: bool = False,
    runner: Run = _run,
) -> int:
    for command in ("git", "gh", "terraform"):
        _require_command(command)
    cloud = _load_script("bootstrap_cloud_preflight", "bootstrap-cloud.py")
    cloud.load_service_account_key(credential_file)
    version = _ensure_release_exists(runner)
    print(f"Using published Model Garden v{version}.")

    _ensure_workspace_and_repo(
        workspace=workspace,
        client_slug=client_slug,
        agent_slug=agent_slug,
        role_title=role_title,
        repo=github_repo,
        runner=runner,
    )
    _ensure_runtime_secrets(
        workspace,
        github_repo,
        runner=runner,
        rotate=rotate_runtime_secrets,
    )

    variables = _repo_variables(github_repo, runner)
    if _cloud_ready(variables):
        mismatches = _cloud_mismatches(
            variables,
            project_id=project_id,
            region=region,
            zone=zone,
            ownership=ownership,
        )
        if mismatches:
            raise ValueError(
                "existing keyless GCP bootstrap does not match this launch; deliberate migration is required: "
                + "; ".join(mismatches)
            )
        print("Reusing existing keyless GCP/OIDC bootstrap.")
    else:
        _run_cloud_bootstrap(
            project_id=project_id,
            repo=github_repo,
            client_slug=client_slug,
            agent_slug=agent_slug,
            credential_file=credential_file,
            region=region,
            zone=zone,
            ownership=ownership,
            runner=runner,
        )

    variables = _repo_variables(github_repo, runner)
    if not _cloud_ready(variables):
        raise ValueError("keyless GCP bootstrap did not publish the complete deployment variable contract")
    mismatches = _cloud_mismatches(
        variables,
        project_id=project_id,
        region=region,
        zone=zone,
        ownership=ownership,
    )
    if mismatches:
        raise ValueError("keyless GCP bootstrap returned unexpected deployment settings: " + "; ".join(mismatches))

    verified = variables.get(VERIFIED_VARIABLE) == "true"
    if not verified:
        prior_runs = _run_ids(github_repo, runner)
        _activate_dev(workspace, runner)
        if no_wait:
            print("DEV activation pushed. Bootstrap credential was not revoked because deployment verification was skipped.")
            return 0
        run_id = _wait_for_new_cloud_run(github_repo, prior_runs, runner=runner)
        print(f"Verified keyless DEV deployment in GitHub Actions run {run_id}.")
        _set_verified(github_repo, runner)
    else:
        print("Reusing previously verified keyless DEV deployment.")

    if credential_file.exists():
        _revoke_bootstrap_key(
            credential_file,
            runner=runner,
            delete_file=not keep_bootstrap_credential,
        )

    for environment in ("dev", "prod"):
        _run_operator(
            ["doctor", str(workspace), "--repo", github_repo, "--environment", environment],
            runner=runner,
            capture=False,
        )
    print("Client bootstrap is self-sustaining: normal infrastructure/runtime deployments now use GitHub OIDC.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_slug")
    parser.add_argument("--github-repo", required=True, help="private client workspace repository owner/name")
    parser.add_argument("--gcp-project", required=True, help="GCP project used for the isolated client runtime")
    parser.add_argument("--bootstrap-credential", required=True, type=pathlib.Path, help="temporary local GCP service-account JSON key")
    parser.add_argument("--workspace", type=pathlib.Path, help="local workspace path; defaults to <client>-ai-workspace")
    parser.add_argument("--agent", default="receptionist")
    parser.add_argument("--role-title")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--zone", default="asia-southeast1-b")
    parser.add_argument("--ownership", choices=("model-garden", "client"), default="model-garden")
    parser.add_argument("--rotate-runtime-secrets", action="store_true", help="prompt for and replace existing dev/prod runtime secret maps")
    parser.add_argument("--keep-bootstrap-credential", action="store_true", help="revoke the cloud key but retain the local JSON file")
    parser.add_argument("--no-wait", action="store_true", help="trigger DEV deployment but do not wait/revoke; useful only for diagnostics")
    args = parser.parse_args()
    workspace = args.workspace or pathlib.Path(f"{args.client_slug}-ai-workspace")
    try:
        return launch(
            client_slug=args.client_slug,
            github_repo=args.github_repo,
            project_id=args.gcp_project,
            credential_file=args.bootstrap_credential,
            workspace=workspace,
            agent_slug=args.agent,
            role_title=args.role_title,
            region=args.region,
            zone=args.zone,
            ownership=args.ownership,
            rotate_runtime_secrets=args.rotate_runtime_secrets,
            keep_bootstrap_credential=args.keep_bootstrap_credential,
            no_wait=args.no_wait,
        )
    except (OSError, ValueError) as exc:
        print(f"Client launch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
