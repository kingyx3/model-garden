#!/usr/bin/env python3
"""Launch a managed Model Garden client with the minimum operator inputs.

A first launch needs a client slug and one temporary local GCP service-account JSON.
The target GCP project defaults to the JSON's project_id and the private workspace repo
defaults to <authenticated GitHub user>/<client>-ai-workspace. After keyless bootstrap is
verified, reruns can omit the deleted bootstrap credential and reuse the recorded GCP
project. Advanced callers can override either value without changing the underlying
launch-client contract.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_bootstrap_cloud():
    path = ROOT / "scripts" / "bootstrap-cloud.py"
    spec = importlib.util.spec_from_file_location("bootstrap_cloud_for_launch", path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


def _github_login() -> str:
    if shutil.which("gh") is None:
        raise ValueError("GitHub CLI 'gh' is required and must be authenticated")
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"cannot determine authenticated GitHub user: {result.stderr.strip() or 'gh auth required'}")
    return result.stdout.strip()


def _existing_gcp_project(repo: str) -> str | None:
    if shutil.which("gh") is None:
        return None
    result = subprocess.run(
        ["gh", "variable", "get", "GCP_PROJECT_ID", "--repo", repo],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def resolve_defaults(
    client_slug: str,
    credential_file: pathlib.Path | None,
    github_repo: str | None,
    gcp_project: str | None,
) -> tuple[str, str]:
    repo = github_repo or f"{_github_login()}/{client_slug}-ai-workspace"
    if gcp_project:
        return repo, gcp_project

    if credential_file is not None and credential_file.is_file():
        cloud = _load_bootstrap_cloud()
        key = cloud.load_service_account_key(credential_file)
        return repo, key["project_id"]

    existing = _existing_gcp_project(repo)
    if existing:
        return repo, existing
    raise ValueError(
        "bootstrap credential is required for the first launch unless --gcp-project is supplied; "
        "after keyless bootstrap the recorded GCP project can be reused without the deleted key"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_slug")
    parser.add_argument("--bootstrap-credential", type=pathlib.Path, help="temporary local GCP service-account JSON key; required for first bootstrap only")
    parser.add_argument("--github-repo", help="override private workspace repo; default: <authenticated-user>/<client>-ai-workspace")
    parser.add_argument("--gcp-project", help="override target project; otherwise bootstrap JSON or existing repository configuration is used")
    parser.add_argument("--workspace", type=pathlib.Path)
    parser.add_argument("--agent", default="receptionist")
    parser.add_argument("--role-title")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--zone", default="asia-southeast1-b")
    parser.add_argument("--ownership", choices=("model-garden", "client"), default="model-garden")
    parser.add_argument("--rotate-runtime-secrets", action="store_true")
    parser.add_argument("--keep-bootstrap-credential", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()

    try:
        repo, project = resolve_defaults(
            args.client_slug,
            args.bootstrap_credential,
            args.github_repo,
            args.gcp_project,
        )
    except (OSError, ValueError) as exc:
        print(f"Client launch preflight failed: {exc}", file=sys.stderr)
        return 1

    print(f"Client repository: {repo}")
    print(f"GCP project: {project}")
    command = [
        sys.executable,
        str(ROOT / "scripts" / "launch-client.py"),
        args.client_slug,
        "--github-repo",
        repo,
        "--gcp-project",
        project,
        "--agent",
        args.agent,
        "--region",
        args.region,
        "--zone",
        args.zone,
        "--ownership",
        args.ownership,
    ]
    if args.bootstrap_credential is not None:
        command.extend(["--bootstrap-credential", str(args.bootstrap_credential)])
    if args.workspace is not None:
        command.extend(["--workspace", str(args.workspace)])
    if args.role_title:
        command.extend(["--role-title", args.role_title])
    if args.rotate_runtime_secrets:
        command.append("--rotate-runtime-secrets")
    if args.keep_bootstrap_credential:
        command.append("--keep-bootstrap-credential")
    if args.no_wait:
        command.append("--no-wait")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
