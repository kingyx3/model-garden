#!/usr/bin/env python3
"""Rebuild one client Hermes profile from workspace desired state and pinned platform metadata.

This is the narrow reproducibility path for the MVP. It verifies that the Model Garden
checkout and Hermes runtime revision match the client's platform.lock.yaml before
compiling the selected Agent and materializing disposable Hermes state. Credentials stay
outside the workspace/profile and remain the deployment environment's responsibility.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Runtime rebuild failed: cannot load platform module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_mapping(path: pathlib.Path, label: str) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Runtime rebuild failed: {label} must be a YAML object")
    return value


def _platform_versions(platform_root: pathlib.Path) -> dict[str, str]:
    modelgarden = (platform_root / "VERSION").read_text(encoding="utf-8").strip()
    hermes: dict[str, str] = {}
    for line in (platform_root / "platform" / "hermes.lock").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            hermes[key.strip()] = value.strip()
    values = {
        "modelgarden": modelgarden,
        "contracts": "v1",
        "hermes": hermes.get("version", ""),
        "hermes_revision": hermes.get("commit", ""),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError(f"Runtime rebuild failed: platform metadata missing {', '.join(missing)}")
    return values


def verify_lock(workspace: pathlib.Path, platform_root: pathlib.Path = ROOT) -> dict[str, str]:
    lock_path = workspace / "platform.lock.yaml"
    if not lock_path.is_file():
        raise ValueError(f"Runtime rebuild failed: missing lockfile {lock_path}")
    lock = _load_mapping(lock_path, "platform.lock.yaml")
    actual = _platform_versions(platform_root)
    for key, expected in actual.items():
        pinned = lock.get(key)
        if not isinstance(pinned, str) or not pinned.strip():
            raise ValueError(f"Runtime rebuild failed: platform.lock.yaml missing {key!r}")
        if pinned.strip() != expected:
            raise ValueError(
                f"Runtime rebuild failed: lock mismatch for {key}: client pins {pinned!r}, "
                f"platform checkout provides {expected!r}"
            )
    return actual


def rebuild(
    workspace: pathlib.Path,
    agent_name: str,
    profile_dir: pathlib.Path,
    library_roots: tuple[pathlib.Path, ...] = (),
    platform_root: pathlib.Path = ROOT,
    dry_run: bool = False,
) -> list[pathlib.Path]:
    verify_lock(workspace, platform_root)
    compiler = _load_module("modelgarden_compile_workspace", platform_root / "scripts" / "compile-workspace.py")
    provisioner = _load_module("modelgarden_provision_hermes", platform_root / "scripts" / "provision-hermes-profile.py")

    compiled = compiler.compile_workspace(workspace, agent_name, library_roots)
    if len(compiled) != 1:
        raise ValueError("Runtime rebuild failed: expected exactly one compiled Agent")
    files = provisioner.build_profile_files(compiled[0])
    return provisioner.apply_profile(profile_dir, files, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=pathlib.Path, help="client workspace containing platform.lock.yaml")
    parser.add_argument("profile_dir", type=pathlib.Path, help="disposable Hermes profile directory")
    parser.add_argument("--agent", default="receptionist", help="Agent to compile and provision")
    parser.add_argument(
        "--library",
        action="append",
        default=[],
        help="curated resource root from this pinned platform release; may be repeated",
    )
    parser.add_argument("--dry-run", action="store_true", help="verify/compile and report profile changes without writing")
    args = parser.parse_args()

    try:
        changed = rebuild(
            args.workspace,
            args.agent,
            args.profile_dir,
            tuple(pathlib.Path(path) for path in args.library),
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    action = "Would update" if args.dry_run else "Updated"
    if changed:
        print(f"{action} {len(changed)} managed Hermes profile file(s) under {args.profile_dir}")
        for path in changed:
            print(f" - {path.as_posix()}")
    else:
        print(f"Hermes profile already matches locked workspace under {args.profile_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
