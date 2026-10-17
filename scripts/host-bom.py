#!/usr/bin/env python3
"""Check that a Docker host was built from the approved rebuild inputs, and record them as evidence (#46).

The GCP Docker-host Terraform module can take an approved boot image (`host_image`) and exact Docker package versions
(`docker_package_versions`) and publishes them as its `host_bom` output. This tool compares what a running host actually has with
that record. It makes no network call and installs nothing.

  terraform -chdir=infra/docker-host/gcp output -json host_bom > host-bom.json
  python3 scripts/host-bom.py collect --image-id "$(gcloud compute disks describe DISK --format='value(sourceImageId)')" > observed.json
  python3 scripts/host-bom.py verify --bom host-bom.json --observed observed.json [--require-pinned]
  python3 scripts/host-bom.py evidence --bom host-bom.json --observed observed.json --commit "$GITHUB_SHA"

``collect`` runs on the host (or reads captured ``dpkg-query`` and ``/etc/os-release`` output with ``--dpkg`` / ``--os-release``).
``verify`` exits 0 when everything the BOM pins matches, 1 when something differs, and, with ``--require-pinned``, also 1 when the
image or a Docker package was left unpinned. ``evidence`` prints the BOM, what was observed and the result with a SHA-256 over the
whole record, for the release evidence pack. A matching host proves it consumed the approved versions; it does not make the host
bit-for-bit reproducible or the supply chain certified.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess  # nosec B404 - runs a fixed dpkg-query argv, never a shell
import sys
from collections.abc import Callable, Mapping
from typing import Any

DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin")
SCHEMA_VERSION = 1
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]{0,63}$")
_IMAGE_ID = re.compile(r"^[0-9]{6,32}$")
_CODENAME = re.compile(r"^[a-z]{3,20}$")


class BomError(ValueError):
    """Raised for a malformed BOM or observation."""


def parse_dpkg(output: str) -> dict[str, str]:
    """``{package: version}`` from ``dpkg-query -W -f='${Package}\\t${Version}\\n'`` lines; only the Docker packages are kept."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.strip().split("\t") if "\t" in line else line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        name, version = parts[0].strip(), parts[1].strip()
        if name in DOCKER_PACKAGES and _VERSION.match(version):
            found[name] = version
    return found


def parse_os_release(text: str) -> str | None:
    """The ``VERSION_CODENAME`` of an ``/etc/os-release`` file, or None."""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "VERSION_CODENAME":
            value = value.strip().strip('"').strip("'")
            return value if _CODENAME.match(value) else None
    return None


def collect(
    *, dpkg_output: str | None = None, os_release: str | None = None, image_id: str | None = None,
    run: Callable[[list[str]], str] | None = None,
) -> dict[str, Any]:
    """What this host has: Docker package versions, the Debian codename and, when told, the boot image id."""
    if dpkg_output is None:
        runner = run or _dpkg
        dpkg_output = runner(["dpkg-query", "-W", "-f=${Package}\t${Version}\n", *DOCKER_PACKAGES])
    if os_release is None:
        try:
            os_release = pathlib.Path("/etc/os-release").read_text(encoding="utf-8")
        except OSError:
            os_release = ""
    if image_id is not None and not _IMAGE_ID.match(str(image_id)):
        raise BomError("the image id is the numeric id Compute Engine reports (sourceImageId)")
    observed: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "docker": parse_dpkg(dpkg_output), "osCodename": parse_os_release(os_release)}
    if image_id is not None:
        observed["imageId"] = str(image_id)
    return observed


def _dpkg(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)  # nosec B603 - fixed argv
    return result.stdout


def check_bom(bom: Any) -> Mapping[str, Any]:
    if not isinstance(bom, Mapping) or bom.get("schemaVersion") != SCHEMA_VERSION:
        raise BomError("not a host BOM (schemaVersion 1) as terraform output -json host_bom prints it")
    image, docker = bom.get("image"), bom.get("docker")
    if not isinstance(image, Mapping) or not isinstance(docker, Mapping) or set(docker) - set(DOCKER_PACKAGES):
        raise BomError("the host BOM holds an image and only the five Docker packages")
    for name, version in docker.items():
        if version != "" and not (isinstance(version, str) and _VERSION.match(version)):
            raise BomError(f"{name}: the approved version must be an apt version string or empty")
    if image.get("id") is not None and not _IMAGE_ID.match(str(image["id"])):
        raise BomError("the BOM's image id must be the numeric Compute Engine image id")
    return bom


def verify(bom: Any, observed: Any, *, require_pinned: bool = False) -> dict[str, Any]:
    """Compare a host with its BOM.

    ``mismatches`` are things the BOM pins that the host does not match (a wrong or missing package, a different image).
    ``unpinned`` are inputs the BOM left to resolve at rebuild time; the host's actual value is recorded for the evidence but nothing
    approved it. ``matches`` is true with no mismatches; ``reproducible`` additionally needs nothing unpinned.
    """
    bom = check_bom(bom)
    if not isinstance(observed, Mapping) or observed.get("schemaVersion") != SCHEMA_VERSION or not isinstance(observed.get("docker"), Mapping):
        raise BomError("not an observation produced by `host-bom.py collect`")
    mismatches: list[str] = []
    unpinned: list[str] = []
    image = bom["image"]
    if image.get("pinned") is True:
        if observed.get("imageId") is None:
            mismatches.append("image: the observation names no image id to compare with the approved image")
        elif str(observed["imageId"]) != str(image.get("id")):
            mismatches.append(f"image: host booted from {observed['imageId']}, approved image is {image.get('id')}")
    else:
        unpinned.append("image: the boot image resolved from a family at rebuild time")
    for name in DOCKER_PACKAGES:
        approved = bom["docker"].get(name, "")
        actual = observed["docker"].get(name)
        if approved == "":
            unpinned.append(f"{name}: no approved version" + (f" (host has {actual})" if actual else " (not installed)"))
        elif actual is None:
            mismatches.append(f"{name}: approved {approved} but it is not installed")
        elif actual != approved:
            mismatches.append(f"{name}: approved {approved} but the host has {actual}")
    matches = not mismatches
    reproducible = matches and not unpinned
    return {"matches": matches, "reproducible": reproducible, "mismatches": mismatches, "unpinned": unpinned,
            "ok": matches and (reproducible or not require_pinned)}


def evidence(bom: Any, observed: Any, *, commit: str | None = None, at: str | None = None, require_pinned: bool = False) -> dict[str, Any]:
    """The record for the release evidence pack: the approved inputs, what the host had, the result and a digest over all of it."""
    result = verify(bom, observed, require_pinned=require_pinned)
    if commit is not None and not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise BomError("commit must be a hexadecimal commit id")
    stamp = at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"schemaVersion": SCHEMA_VERSION, "recordedAt": stamp, "commit": commit, "bom": bom, "observed": observed, "result": result,
            "limits": "Shows the host consumed the approved image and package versions; does not make it bit-for-bit reproducible or certify the supply chain."}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**body, "sha256": digest}


def _read(path: str) -> Any:
    raw = sys.stdin.read() if path == "-" else pathlib.Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BomError(f"{path} is not JSON") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    gather = commands.add_parser("collect", help="print what this host has as JSON")
    gather.add_argument("--dpkg", help="a file holding dpkg-query output, instead of running it")
    gather.add_argument("--os-release", help="a file holding /etc/os-release, instead of reading the local one")
    gather.add_argument("--image-id", help="the numeric boot image id (gcloud compute disks describe ... --format='value(sourceImageId)')")
    for name in ("verify", "evidence"):
        sub = commands.add_parser(name)
        sub.add_argument("--bom", required=True, help="terraform output -json host_bom, or - for standard input")
        sub.add_argument("--observed", required=True, help="the JSON `collect` printed")
        sub.add_argument("--require-pinned", action="store_true", help="also fail when the image or a Docker package was left unpinned")
        if name == "evidence":
            sub.add_argument("--commit")
            sub.add_argument("--at", help="recording time (default: now, UTC)")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            dpkg = pathlib.Path(args.dpkg).read_text(encoding="utf-8") if args.dpkg else None
            os_release = pathlib.Path(args.os_release).read_text(encoding="utf-8") if args.os_release else None
            print(json.dumps(collect(dpkg_output=dpkg, os_release=os_release, image_id=args.image_id), indent=2, sort_keys=True))
            return 0
        bom, observed = _read(args.bom), _read(args.observed)
        if args.command == "verify":
            result = verify(bom, observed, require_pinned=args.require_pinned)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 1
        record = evidence(bom, observed, commit=args.commit, at=args.at, require_pinned=args.require_pinned)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0 if record["result"]["ok"] else 1
    except (BomError, OSError) as exc:
        print(f"Host BOM check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
