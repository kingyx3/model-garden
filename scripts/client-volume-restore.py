#!/usr/bin/env python3
"""Fail-closed operator verification and empty-volume restore of age snapshots (#230).

Never decrypt to a host file, overwrite existing state, auto-start a service, or
treat this as proof of a client recovery drill. Docker access and a separately
held age identity are prerequisites; operator identity is enforced by host RBAC.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tarfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_restore_snapshot", ROOT / "scripts" / "client-volume-snapshot.py")
SNAPSHOT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SNAPSHOT)

SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 131072
MAX_TAR_BYTES = 100 * 1024**3
MAX_MEMBERS = 250000


class RestoreError(ValueError):
    pass


def _private_input(path: pathlib.Path, *, label: str, directory: bool = False) -> os.stat_result:
    """Refuse shared, linked, foreign-owned recovery inputs before decrypt/write.

    Snapshot/read-back commands create 0700 directories and 0600 files. A
    hardlinked identity or ciphertext can be changed through another pathname;
    a world-readable manifest can disclose client recovery metadata.
    """
    try:
        info = path.lstat()
    except OSError as exc:
        raise RestoreError(f"{label} must be an existing private input") from exc
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not kind_ok or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise RestoreError(f"{label} must be private and owner-owned (no symlink or hardlink)")
    return info


def _private_identity(identity: pathlib.Path) -> None:
    info = _private_input(identity, label="age identity")
    if info.st_size > 16384:
        raise RestoreError("age identity exceeds permitted key-file size")


def _manifest_seal(source: pathlib.Path) -> tuple[tuple[int, ...], bytes]:
    """Pin the exact private manifest inode and bytes before archive verification.

    The original manifest is the trust anchor for every later ciphertext digest.
    Never re-read a mutable manifest to authorize different archive contents.
    """
    _private_input(source, label="backup source directory", directory=True)
    path = source / "manifest.json"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise RestoreError("backup manifest is missing or unsafe") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077
                or not 0 < info.st_size <= MAX_MANIFEST_BYTES):
            raise RestoreError("backup manifest must be private and bounded")
        payload = handle.read(MAX_MANIFEST_BYTES + 1)
    if not 0 < len(payload) <= MAX_MANIFEST_BYTES:
        raise RestoreError("backup manifest size is invalid")
    token = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
             info.st_ctime_ns, int.from_bytes(hashlib.sha256(payload).digest(), "big"))
    return token, payload


def _require_original_manifest(source: pathlib.Path, token: tuple[int, ...]) -> None:
    try:
        current, _ = _manifest_seal(source)
    except RestoreError as exc:
        raise RestoreError("backup manifest changed after verification; keep stack stopped") from exc
    if current != token:
        raise RestoreError("backup manifest changed after verification; keep stack stopped")


def _validated_manifest(compose: pathlib.Path, project: str, source: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    SNAPSHOT._name(project, "client project")
    _private_input(source, label="backup source directory", directory=True)
    manifest_path = source / "manifest.json"
    if _private_input(manifest_path, label="backup manifest").st_size > MAX_MANIFEST_BYTES:
        raise RestoreError("oversized backup manifest")
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RestoreError("invalid backup manifest") from exc
    if not isinstance(doc, dict) or doc.get("schemaVersion") != 1 or doc.get("type") != "stopped-compose-volume-snapshot":
        raise RestoreError("unsupported backup manifest schema")
    if doc.get("clientProject") != project:
        raise RestoreError("snapshot client does not match requested destination client project")
    if not isinstance(doc.get("ageRecipientSha256"), str) or not SHA256.fullmatch(doc["ageRecipientSha256"]):
        raise RestoreError("invalid backup recipient fingerprint")
    expected = SNAPSHOT.inventory(compose)
    records = doc.get("volumes")
    if not isinstance(records, list) or len(records) != len(expected):
        raise RestoreError("snapshot volume inventory is incomplete")
    seen: set[str] = set()
    verified: list[tuple[str, pathlib.Path]] = []
    for record in records:
        if not isinstance(record, dict):
            raise RestoreError("invalid backup volume entry")
        name = record.get("volume")
        if not isinstance(name, str) or name not in expected or name in seen:
            raise RestoreError("snapshot volume names are missing, duplicated or unrecognized")
        seen.add(name)
        if record.get("object") != f"{name}.tar.age":
            raise RestoreError("snapshot volume object name does not match inventory")
        size = record.get("bytes")
        digest = record.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise RestoreError("invalid encrypted archive size")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise RestoreError("invalid encrypted archive digest")
        path = source / f"{name}.tar.age"
        if _private_input(path, label="encrypted archive").st_size != size:
            raise RestoreError("encrypted archive digest/size mismatch; refuse restoration")
        measured, actual_size = SNAPSHOT._digest(path)
        if measured != digest or actual_size != size:
            raise RestoreError("encrypted archive digest/size mismatch; refuse restoration")
        verified.append((name, path))
    return sorted(verified)


def _safe_tar_member(member: tarfile.TarInfo) -> bool:
    path = pathlib.PurePosixPath(member.name)
    if not member.name or path.is_absolute() or ".." in path.parts or "\x00" in member.name:
        return False
    # Snapshot captures plain tar; symlinks/hardlinks/devices could escape an
    # extraction root even if the lexical member path looks benign.
    return member.isdir() or member.isfile()


def _validate_plaintext_tar(archive: pathlib.Path, identity: pathlib.Path) -> None:
    """Stream-decrypt and inspect every tar member without extracting or logging it."""
    command = ["age", "--decrypt", "--identity", str(identity), str(archive)]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RestoreError("age decryptor is not installed or cannot start") from exc
    members = 0
    total = 0
    try:
        assert process.stdout is not None
        with process.stdout:
            with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
                for member in stream:
                    members += 1
                    total += member.size
                    if not _safe_tar_member(member) or member.size < 0 or total > MAX_TAR_BYTES or members > MAX_MEMBERS:
                        raise RestoreError("unsafe tar member or archive exceeds limits")
        if process.wait(timeout=900) != 0:
            raise RestoreError("age authentication/decryption failed")
    except (tarfile.TarError, EOFError, OSError, subprocess.TimeoutExpired) as exc:
        raise RestoreError("archive is not a valid authenticated tar stream") from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def verify(compose: pathlib.Path, project: str, source: pathlib.Path, identity: pathlib.Path) -> list[str]:
    """Offline verification: source project, full manifest, digest and safe authenticated tar."""
    _private_identity(identity)
    archives = _validated_manifest(compose, project, source)
    for _name, path in archives:
        _validate_plaintext_tar(path, identity)
    return [name for name, _path in archives]


def _docker(*args: str) -> str:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RestoreError("target volume is not empty or cannot be inspected") from exc
    if result.returncode:
        raise RestoreError("target volume is not empty or cannot be inspected")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _assert_empty(docker_volume: str, image: str) -> None:
    _docker("run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=volume,src={docker_volume},dst=/restore,readonly",
            "--entrypoint", "sh", image, "-c", 'test -z "$(ls -A /restore)"')


def _extract(docker_volume: str, archive: pathlib.Path, identity: pathlib.Path, image: str) -> None:
    # Decrypted TAR bytes travel only over an anonymous pipe into an isolated
    # Docker helper. The original archive has already been fully verified.
    try:
        decrypt = subprocess.Popen(
            ["age", "--decrypt", "--identity", str(identity), str(archive)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert decrypt.stdout is not None
        with decrypt.stdout:
            extract = subprocess.run(
                ["docker", "run", "--rm", "--interactive", "--network", "none",
                 "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--mount", f"type=volume,src={docker_volume},dst=/restore",
                 "--entrypoint", "tar", image, "--no-same-owner", "-C", "/restore", "-xf", "-"],
                stdin=decrypt.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=3600,
            )
        authenticated = decrypt.wait(timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if "decrypt" in locals() and decrypt.poll() is None:
            decrypt.kill()
            decrypt.wait()
        raise RestoreError("restore stream failed; target may be partially restored; keep stack stopped") from exc
    if extract.returncode or authenticated:
        raise RestoreError("restore did not complete; target may be partially restored; keep stack stopped")


def restore(
    compose: pathlib.Path, project: str, source: pathlib.Path, identity: pathlib.Path,
    image: str, *, apply: bool = False, confirmation: str | None = None,
) -> list[str]:
    if not SNAPSHOT.IMAGE.fullmatch(image):
        raise RestoreError("restore helper image must be pinned to an immutable sha256 digest")
    if apply and confirmation != project:
        raise RestoreError("restore requires --confirm-project matching the exact client project")
    if not apply:
        return verify(compose, project, source, identity)
    original_token, original_bytes = _manifest_seal(source)
    names = verify(compose, project, source, identity)
    _require_original_manifest(source, original_token)
    # The verified manifest is pinned before decryption, not reloaded from a
    # path that a same-UID process could replace between checks.
    original_records = json.loads(original_bytes)["volumes"]
    expected_digests = {row["volume"]: (row["sha256"], row["bytes"]) for row in original_records}
    # Fail before ANY writes: every target must already exist, belong to the
    # exact Compose project, be empty, and have no running containers.
    try:
        SNAPSHOT.assert_quiescent(project)
        owned = [(name, SNAPSHOT.assert_volume_owner(project, name)) for name in names]
        for name, volume in owned:
            SNAPSHOT.assert_volume_exclusive(project, name)
            _assert_empty(volume, image)
        for name, volume in owned:
            SNAPSHOT.assert_quiescent(project)
            SNAPSHOT.assert_volume_exclusive(project, name)
            _assert_empty(volume, image)
            SNAPSHOT.assert_volume_exclusive(project, name)
            archive = source / f"{name}.tar.age"
            _require_original_manifest(source, original_token)
            _private_input(archive, label="encrypted archive")
            if SNAPSHOT._digest(archive) != expected_digests[name]:
                raise RestoreError("encrypted archive changed during restore; keep stack stopped")
            _require_original_manifest(source, original_token)
            _extract(volume, archive, identity, image)
            _require_original_manifest(source, original_token)
            SNAPSHOT.assert_volume_exclusive(project, name)
        SNAPSHOT.assert_quiescent(project)
        for name, _volume in owned:
            SNAPSHOT.assert_volume_exclusive(project, name)
        _require_original_manifest(source, original_token)
    except SNAPSHOT.SnapshotError as exc:
        raise RestoreError(str(exc)) from exc
    # Deliberately no docker compose start: the authorized operator audits
    # restored data and decides whether/when to resume the client workload.
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "restore"))
    parser.add_argument("--compose", required=True, type=pathlib.Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--identity", required=True, type=pathlib.Path)
    parser.add_argument("--image", help="tar+sh helper pinned as image@sha256:...")
    parser.add_argument("--confirm-project")
    args = parser.parse_args(argv)
    try:
        if args.command == "restore" and not args.image:
            raise RestoreError("restore requires --image with pinned digest")
        names = (verify(args.compose, args.project, args.source, args.identity)
                 if args.command == "verify"
                 else restore(args.compose, args.project, args.source, args.identity, args.image,
                              apply=True, confirmation=args.confirm_project))
        print(json.dumps({"clientProject": args.project, "verifiedVolumes": len(names),
                          "applied": args.command == "restore"}, sort_keys=True))
        return 0
    except (RestoreError, SNAPSHOT.SnapshotError, OSError, ValueError) as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
