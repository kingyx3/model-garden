#!/usr/bin/env python3
"""Bootstrap one cloud account/project once, then switch Model Garden to keyless GitHub OIDC.

The first reference implementation is GCP because its service-account JSON -> Workload
Identity Federation lifecycle directly proves the intended contract. The temporary JSON
key is read only from the operator-supplied local path and is never copied into GitHub,
Terraform variables, state, generated client files, or command arguments.

After Terraform creates the state bucket, GitHub Workload Identity Federation provider,
keyless deployer service account and runtime identity, this command stores only non-secret
outputs as GitHub repository variables. Subsequent client deployments authenticate from
GitHub Actions with OIDC and no cloud service-account key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGION = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
ZONE = re.compile(r"^[a-z]+-[a-z]+[0-9]+-[a-z]$")

Run = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    command: list[str],
    *,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise ValueError(f"required command is not installed or not on PATH: {name}")


def _validate_inputs(project_id: str, github_repo: str, region: str, zone: str, owner: str) -> None:
    if not PROJECT.fullmatch(project_id):
        raise ValueError("GCP project ID must use the normal lowercase project-id format")
    if not REPO.fullmatch(github_repo):
        raise ValueError("GitHub repository must be owner/name")
    if not REGION.fullmatch(region):
        raise ValueError("GCP region is invalid")
    if not ZONE.fullmatch(zone) or not zone.startswith(region + "-"):
        raise ValueError("GCP zone must belong to the selected region")
    if owner not in {"model-garden", "client"}:
        raise ValueError("infrastructure owner must be model-garden or client")


def load_service_account_key(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("bootstrap credential must be a regular local JSON file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("bootstrap credential is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("type") != "service_account":
        raise ValueError("bootstrap credential must be a GCP service-account JSON key")
    required = ("project_id", "private_key_id", "private_key", "client_email")
    for field in required:
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"bootstrap credential is missing {field}")
    return {field: value[field] for field in required}


def _state_root(project_id: str, github_repo: str, base: pathlib.Path | None = None) -> pathlib.Path:
    digest = hashlib.sha256(github_repo.encode("utf-8")).hexdigest()[:8]
    root = base or pathlib.Path.home() / ".model-garden" / "bootstrap"
    return root / f"gcp-{project_id}-{digest}"


def _sync_bootstrap_module(destination: pathlib.Path) -> None:
    source = ROOT / "infra" / "bootstrap" / "gcp"
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.tf"):
        shutil.copy2(path, destination / path.name)


def _terraform_outputs(raw: str) -> dict[str, str]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Terraform bootstrap output was not valid JSON") from exc
    if not isinstance(values, dict):
        raise ValueError("Terraform bootstrap output must be an object")
    result: dict[str, str] = {}
    for key in (
        "project_id",
        "region",
        "terraform_state_bucket",
        "workload_identity_provider",
        "deployer_service_account",
        "runtime_service_account",
        "infrastructure_owner",
    ):
        item = values.get(key)
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError(f"Terraform bootstrap output is missing {key}")
        result[key] = value
    return result


def github_variables(outputs: dict[str, str], *, zone: str) -> dict[str, str]:
    return {
        "MODEL_GARDEN_CLOUD_PROVIDER": "gcp",
        "MODEL_GARDEN_INFRA_OWNERSHIP": outputs["infrastructure_owner"],
        "MODEL_GARDEN_TF_STATE_BUCKET": outputs["terraform_state_bucket"],
        "GCP_PROJECT_ID": outputs["project_id"],
        "GCP_REGION": outputs["region"],
        "GCP_ZONE": zone,
        "GCP_WORKLOAD_IDENTITY_PROVIDER": outputs["workload_identity_provider"],
        "GCP_DEPLOY_SERVICE_ACCOUNT": outputs["deployer_service_account"],
        "GCP_RUNTIME_SERVICE_ACCOUNT": outputs["runtime_service_account"],
    }


def _checked(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise ValueError(f"{label} failed: {detail}")
    return result.stdout


def bootstrap_gcp(
    *,
    project_id: str,
    github_repo: str,
    region: str,
    zone: str,
    infrastructure_owner: str,
    credential_path: pathlib.Path,
    state_base: pathlib.Path | None = None,
    dry_run: bool = False,
    runner: Run = _run,
) -> dict[str, str]:
    _validate_inputs(project_id, github_repo, region, zone, infrastructure_owner)
    key = load_service_account_key(credential_path)

    if dry_run:
        return {
            "credential_service_account": key["client_email"],
            "credential_project": key["project_id"],
            "target_project": project_id,
            "github_repository": github_repo,
            "region": region,
            "zone": zone,
            "infrastructure_owner": infrastructure_owner,
            "state_directory": str(_state_root(project_id, github_repo, state_base)),
        }

    _require_command("terraform")
    _require_command("gh")

    state_root = _state_root(project_id, github_repo, state_base)
    module_dir = state_root / "module"
    _sync_bootstrap_module(module_dir)

    env = os.environ.copy()
    env["GOOGLE_APPLICATION_CREDENTIALS"] = str(credential_path.resolve())
    env.pop("GOOGLE_CREDENTIALS", None)
    env.pop("GCP_CREDENTIALS", None)

    _checked(
        runner(["terraform", "init", "-input=false"], cwd=module_dir, env=env),
        "Terraform bootstrap init",
    )
    _checked(
        runner(
            [
                "terraform",
                "apply",
                "-auto-approve",
                "-input=false",
                f"-var=project_id={project_id}",
                f"-var=region={region}",
                f"-var=github_repository={github_repo}",
                f"-var=infrastructure_owner={infrastructure_owner}",
            ],
            cwd=module_dir,
            env=env,
        ),
        "Terraform bootstrap apply",
    )
    output_raw = _checked(
        runner(["terraform", "output", "-json"], cwd=module_dir, env=env),
        "Terraform bootstrap output",
    )
    outputs = _terraform_outputs(output_raw)
    variables = github_variables(outputs, zone=zone)

    for name, value in sorted(variables.items()):
        _checked(
            runner(["gh", "variable", "set", name, "--repo", github_repo, "--body", value]),
            f"GitHub variable {name}",
        )

    return {
        **outputs,
        "zone": zone,
        "credential_service_account": key["client_email"],
        "credential_private_key_id": key["private_key_id"],
        "bootstrap_state_directory": str(state_root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("gcp",), help="cloud provider; GCP is the current reference implementation")
    parser.add_argument("--project", required=True, help="target GCP project ID")
    parser.add_argument("--repo", required=True, help="client workspace GitHub repository owner/name")
    parser.add_argument("--credential-file", required=True, type=pathlib.Path, help="temporary local GCP service-account JSON key")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--zone", default="asia-southeast1-b")
    parser.add_argument("--ownership", choices=("model-garden", "client"), default="model-garden")
    parser.add_argument("--state-dir", type=pathlib.Path, help="optional non-repository directory for bootstrap Terraform state")
    parser.add_argument("--dry-run", action="store_true", help="validate inputs and credential shape without cloud/GitHub writes")
    args = parser.parse_args()

    try:
        result = bootstrap_gcp(
            project_id=args.project,
            github_repo=args.repo,
            region=args.region,
            zone=args.zone,
            infrastructure_owner=args.ownership,
            credential_path=args.credential_file,
            state_base=args.state_dir,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        print(f"Cloud bootstrap failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(result, indent=2, sort_keys=True))
        print("Dry run only; no Terraform or GitHub changes were made.")
        return 0

    print("Keyless Model Garden cloud bootstrap completed.")
    print(f"GitHub repository: {args.repo}")
    print(f"Target project: {args.project} ({args.ownership})")
    print(f"Terraform bootstrap state: {result['bootstrap_state_directory']}")
    print("No cloud credential was copied into GitHub or the client workspace.")
    print("After confirming one GitHub OIDC deployment succeeds, revoke the bootstrap key in GCP and delete the local JSON file.")
    print(
        "Suggested revocation: gcloud iam service-accounts keys delete "
        f"{result['credential_private_key_id']} --iam-account {result['credential_service_account']} "
        f"--project {args.project}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
