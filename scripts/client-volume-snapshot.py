#!/usr/bin/env python3
"""Offline, encrypted snapshots of a stopped client's Compose named volumes (#230).

A snapshot is not an automated backup or a tested restore. Operators must stop
the client stack, prevent other deployments, and keep the age private key offline.
No client payload is written as plaintext on the host by this script.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
from typing import Any

import yaml

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
IMAGE = re.compile(r"^[a-zA-Z0-9_.:/-]+@sha256:[0-9a-f]{64}$")
RECIPIENT = re.compile(r"^age1[a-z0-9]{50,}$")
LABEL_PROJECT = "com.docker.compose.project"
LABEL_VOLUME = "com.docker.compose.volume"
MAX_VOLUME_ATTACHMENTS = 64
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
VOLUME_MOUNT_INSPECT_FORMAT = '{{.Id}}|{{index .Config.Labels "com.docker.compose.project"}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.RestartPolicy.Name}}'


class SnapshotError(ValueError):
    pass


def _name(value: str, label: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise SnapshotError(f"invalid {label}; expected a Docker Compose-safe name")
    return value


def inventory(compose_path: pathlib.Path) -> list[str]:
    """Fail closed on external, ambiguous, unnamed or untracked mutable state."""
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        raise SnapshotError("Compose file must contain a services mapping")
    declared = doc.get("volumes")
    if not isinstance(declared, dict) or not declared:
        raise SnapshotError("no named volumes declared; cannot prove Tier 0 coverage")
    for name, spec in declared.items():
        _name(name, "volume name")
        if spec is not None and (not isinstance(spec, dict) or
                                 any(key in spec for key in ("external", "name", "driver_opts"))):
            raise SnapshotError(f"volume {name} has unmanaged/external storage")
    mounted: set[str] = set()
    for service, cfg in doc["services"].items():
        if not isinstance(cfg, dict):
            raise SnapshotError(f"invalid service {service}")
        for mount in cfg.get("volumes", []):
            if isinstance(mount, str):
                source, sep, remaining = mount.partition(":")
                if not sep:
                    raise SnapshotError(f"anonymous volume in service {service}")
                target, *options = remaining.split(":")
                readonly = "ro" in (options[0].split(",") if options else [])
                if source.startswith(("/", "./", "../", "~")):
                    if not readonly:
                        raise SnapshotError(f"writable host bind mount in {service} cannot be snapshotted safely")
                    continue
                _name(source, "named volume source")
                if source not in declared:
                    raise SnapshotError(f"service {service} uses undeclared volume {source}")
                mounted.add(source)
            elif isinstance(mount, dict):
                typ = mount.get("type")
                source = mount.get("source")
                if typ == "bind":
                    if not mount.get("read_only", False):
                        raise SnapshotError(f"writable host bind mount in {service} cannot be snapshotted safely")
                    continue
                if typ != "volume" or source is None:
                    raise SnapshotError(f"untracked or anonymous mount in service {service}")
                _name(source, "named volume source")
                if source not in declared:
                    raise SnapshotError(f"service {service} uses undeclared volume {source}")
                mounted.add(source)
            else:
                raise SnapshotError(f"invalid volume mount in {service}")
    if set(declared) != mounted:
        raise SnapshotError("unmounted declared volumes cannot be attributed: " + ", ".join(sorted(set(declared) - mounted)))
    return sorted(mounted)


def _docker(*args: str) -> str:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True,
                                check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("Docker inspection unavailable or timed out") from exc
    if result.returncode:
        raise SnapshotError("Docker inspection failed (check daemon access and client project)")
    return result.stdout.strip()


def assert_quiescent(project: str) -> None:
    # Avoid even reading a live SQLite/Hermes store. Lock out concurrent deploys
    # operationally: this check cannot prevent a separate operator restarting it.
    if _docker("ps", "--filter", f"label={LABEL_PROJECT}={project}", "--format", "{{.ID}}"):
        raise SnapshotError("client containers are running; stop all services and freeze deployment first")


def assert_volume_owner(project: str, name: str) -> str:
    actual = f"{project}_{name}"
    labels = json.loads(_docker("volume", "inspect", actual, "--format", "{{json .Labels}}"))
    if not isinstance(labels, dict) or labels.get(LABEL_PROJECT) != project or labels.get(LABEL_VOLUME) != name:
        raise SnapshotError(f"volume {name} has no matching Compose project/volume ownership labels")
    return actual


def assert_volume_exclusive(project: str, name: str) -> None:
    """Reject other-project, unlabelled or restartable mounts on this Docker daemon.

    This is read-only evidence at inspection time, NOT a cross-host/atomic freeze.
    Check before, between and after archive stages; retain external restart controls.
    """
    _name(project, "client project")
    _name(name, "volume name")
    actual = f"{project}_{name}"
    listing = _docker("ps", "--all", "--no-trunc", "--filter", f"volume={actual}",
                      "--format", "{{.ID}}")
    if len(listing) > MAX_VOLUME_ATTACHMENTS * 65:
        raise SnapshotError("volume attachment inventory is unbounded")
    ids = listing.splitlines()
    if len(ids) > MAX_VOLUME_ATTACHMENTS or len(set(ids)) != len(ids) or \
       any(not CONTAINER_ID.fullmatch(cid) for cid in ids):
        raise SnapshotError("volume attachment inventory is invalid or unbounded")
    for cid in ids:
        # Only inspect identity, project label, state and restart policy; never Env.
        fields = _docker("inspect", "--type", "container", "--format",
                         VOLUME_MOUNT_INSPECT_FORMAT, cid).strip().split("|")
        if len(fields) != 5 or fields[0] != cid or fields[1] != project:
            raise SnapshotError("volume is attached to a foreign or unverified container")
        if fields[2] not in ("created", "exited") or fields[3] != "false" or fields[4] != "no":
            raise SnapshotError("volume is attached to a running or restartable container")


def _digest(path: pathlib.Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest(), path.stat().st_size


def snapshot(
    compose: pathlib.Path, project: str, output: pathlib.Path, recipient: str, image: str,
) -> dict[str, Any]:
    """Stream each tar directly from a read-only Docker mount into age encryption."""
    _name(project, "client project")
    if not RECIPIENT.fullmatch(recipient):
        raise SnapshotError("age recipient must be a complete public age1... key")
    if not IMAGE.fullmatch(image):
        raise SnapshotError("snapshot helper image must be pinned by sha256 digest")
    volumes = inventory(compose)
    if not shutil.which("docker") or not shutil.which("age"):
        raise SnapshotError("operator host requires docker and age; no fallback to plaintext")
    assert_quiescent(project)
    owned = [(name, assert_volume_owner(project, name)) for name in volumes]
    for name, _ in owned:
        assert_volume_exclusive(project, name)
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o022:
        raise SnapshotError("backup parent directory must exist, not be a symlink and not be group/world writable")
    if output.exists() or output.is_symlink():
        raise SnapshotError("snapshot destination must not exist (never overwrite prior evidence)")
    previous_umask = os.umask(0o077)
    try:
        output.mkdir(mode=0o700)
        records: list[dict[str, Any]] = []
        for name, docker_name in owned:
            assert_quiescent(project)
            assert_volume_exclusive(project, name)
            target = output / f"{name}.tar.age"
            tar_cmd = [
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--mount", f"type=volume,src={docker_name},dst=/source,readonly",
                "--entrypoint", "tar", image, "-C", "/source", "-cf", "-", ".",
            ]
            with subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as tar:
                assert tar.stdout is not None
                with subprocess.Popen(
                    ["age", "-r", recipient, "-o", str(target)],
                    stdin=tar.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ) as encrypt:
                    tar.stdout.close()
                    try:
                        enc_status = encrypt.wait(timeout=3600)
                        tar_status = tar.wait(timeout=30)
                    except subprocess.TimeoutExpired as exc:
                        encrypt.kill()
                        tar.kill()
                        raise SnapshotError("encrypted snapshot timed out") from exc
                if enc_status or tar_status:
                    raise SnapshotError(f"snapshot encryption/archival failed for volume {name}")
            target.chmod(0o600)
            digest, size = _digest(target)
            if not size:
                raise SnapshotError(f"encrypted snapshot is empty for volume {name}")
            records.append({"volume": name, "object": target.name, "sha256": digest, "bytes": size})
            assert_quiescent(project)
            assert_volume_exclusive(project, name)
        for name, _ in owned:
            assert_volume_exclusive(project, name)
        manifest = {
            "schemaVersion": 1, "clientProject": project, "type": "stopped-compose-volume-snapshot",
            "createdAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ageRecipientSha256": hashlib.sha256(recipient.encode("ascii")).hexdigest(),
            "volumes": records,
        }
        with (output / "manifest.json").open("x", encoding="utf-8") as f:
            f.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        (output / "manifest.json").chmod(0o600)
        return manifest
    finally:
        os.umask(previous_umask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inventory", "snapshot"])
    parser.add_argument("--compose", type=pathlib.Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--recipient")
    parser.add_argument("--image", help="locally available tar-capable helper at repo@sha256:<64 hex>")
    args = parser.parse_args(argv)
    try:
        _name(args.project, "client project")
        if args.command == "inventory":
            print(json.dumps({"clientProject": args.project, "volumes": inventory(args.compose)}, sort_keys=True))
        else:
            if args.output is None or args.recipient is None or args.image is None:
                raise SnapshotError("snapshot requires --output, --recipient and --image")
            result = snapshot(args.compose, args.project, args.output, args.recipient, args.image)
            print(json.dumps({"clientProject": result["clientProject"], "volumeCount": len(result["volumes"]),
                              "manifest": str(args.output / "manifest.json")}, sort_keys=True))
        return 0
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"Snapshot refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
