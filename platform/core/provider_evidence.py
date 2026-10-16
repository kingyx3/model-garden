"""Per-provider evidence manifest and go-live gate.

Whether a provider may be called "live" has to be demonstrated, not inferred from unit tests (#117, #112). This module turns
the integration registry (``contracts/v1/integrations.json``) into an evidence manifest, one entry per provider, and decides
for each whether a go-live claim is allowed. It reads files only, holds no credential and calls no provider.

An entry holds: the provider's family, tier, method and registry status; the adapter file and its SHA-256 (the adapter
"commit" in a source tree that is not necessarily a git checkout); the automated test files that exercise that adapter and how
many tests they hold; the reviewed scope set; the retained live tenant proof and its state; the limitations that still apply;
and ``goLive``: whether a go-live claim is allowed and, if not, every blocker.

- **Live proof is a retained, redacted file.** ``assurance/provider-proof/<provider id>.json`` (named by the registry's
  ``proof`` field for a ``live`` provider) holds ``provedAt`` and ``expiresAt`` (ISO 8601), the SHA-256 of the adapter it
  proved, the scopes it exercised, the person who recorded it, evidence references (run ids, artefact names; never content) and
  ``redacted: true``. A tenant identifier, token or any other key is refused. A proof is **bound to the adapter it proved**: if
  the adapter file changes, the proof no longer counts and the provider is blocked until it is re-proven.
- **Proof expires.** An expired proof is a blocker, not a deletion: the manifest says it expired and when.
- **What a go-live claim needs.** A shipped adapter (``built-credential-free`` or ``live``), automated adapter tests, and, for a
  provider that reaches a third-party tenant (tier 1 and above), a valid, unexpired proof for the current adapter whose scopes
  match the reviewed scope set; and the registry itself says ``live``. A Tier 0 provider reaches no third-party tenant, so it
  needs tests but no tenant proof; it still carries the limitation that no deployment evidence is retained.
- **Structural check.** ``check`` fails only for something that makes the registry's claims untrue now: a ``live`` provider with
  no proof file, a malformed or secret-bearing proof, a proof for another provider or adapter, or scopes that differ. An
  *expired* proof does not break unrelated work; it blocks go-live for that provider and shows in the manifest.

Not done: producing a proof (that needs a real tenant and a person), checking provider-side logs, or publishing the manifest to
the integration directory.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
from collections.abc import Mapping
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROOF_DIR = "assurance/provider-proof"
SCHEMA_VERSION = 1
BUILT = ("live", "built-credential-free")
PROOF_KEYS = {"schemaVersion", "provider", "provedAt", "expiresAt", "adapterSha256", "scopes", "recordedBy", "evidence", "redacted", "limitations"}
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_TEXT = re.compile(r"^[^\x00-\x1f]{1,300}$")
MAX_EVIDENCE = 50


def _read_registry(root: pathlib.Path) -> dict[str, Any]:
    return json.loads((root / "contracts/v1/integrations.json").read_text(encoding="utf-8"))


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def reviewed_scopes(entry: Mapping[str, Any]) -> list[str]:
    return sorted({scope for scopes in (entry.get("scopeByTool") or {}).values() for scope in scopes})


def adapter_tests(root: pathlib.Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    """The test files that load this adapter's source (by its repository path) and how many tests they hold."""
    adapter = entry.get("adapter")
    files: dict[str, int] = {}
    if isinstance(adapter, str):
        for path in sorted((root / "tests").glob("test_*.py")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if adapter in text:
                files[path.name] = len(re.findall(r"^\s+def test_", text, flags=re.MULTILINE))
    return {"files": sorted(files), "tests": sum(files.values())}


def validate_proof(root: pathlib.Path, entry: Mapping[str, Any], now: dt.datetime) -> dict[str, Any]:
    """``{state, errors, provedAt, expiresAt}``; state is ``none``, ``valid``, ``expired`` or ``invalid``."""
    name = entry["id"]
    path = root / PROOF_DIR / f"{name}.json"
    if not path.is_file():
        return {"state": "none", "errors": []}
    errors: list[str] = []
    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "invalid", "errors": ["proof file is not readable JSON"]}
    if not isinstance(proof, dict):
        return {"state": "invalid", "errors": ["proof must be a JSON object"]}
    extra = sorted(set(proof) - PROOF_KEYS)
    if extra:
        errors.append("proof holds fields that are not allowed (" + ", ".join(extra) + "); a proof keeps no tenant identifier, token or content")
    if proof.get("schemaVersion") != SCHEMA_VERSION:
        errors.append("unsupported proof schemaVersion")
    if proof.get("provider") != name:
        errors.append("proof names a different provider")
    if proof.get("redacted") is not True:
        errors.append("proof must state redacted: true")
    proved, expires = _time(proof.get("provedAt")), _time(proof.get("expiresAt"))
    if proved is None or expires is None:
        errors.append("provedAt and expiresAt must be ISO 8601 times with a zone")
    elif expires <= proved:
        errors.append("expiresAt must be after provedAt")
    elif proved > now:
        errors.append("provedAt is in the future")
    if not (isinstance(proof.get("recordedBy"), str) and _PERSON.match(proof["recordedBy"])):
        errors.append("recordedBy must name the person who recorded the proof")
    evidence = proof.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE or not all(isinstance(item, str) and _TEXT.match(item) for item in evidence):
        errors.append(f"evidence must list 1 to {MAX_EVIDENCE} short references (run ids or artefact names)")
    limitations = proof.get("limitations", [])
    if not isinstance(limitations, list) or not all(isinstance(item, str) and _TEXT.match(item) for item in limitations):
        errors.append("limitations must be a list of short sentences")
    adapter = entry.get("adapter")
    if not (isinstance(proof.get("adapterSha256"), str) and _SHA.match(proof["adapterSha256"])):
        errors.append("adapterSha256 must be the adapter's SHA-256")
    elif isinstance(adapter, str) and (root / adapter).is_file() and proof["adapterSha256"] != _digest(root / adapter):
        errors.append("the adapter changed since this proof was recorded; it must be proven again")
    scopes = proof.get("scopes")
    if not isinstance(scopes, list) or sorted(set(scopes)) != reviewed_scopes(entry):
        errors.append("proof scopes must equal the reviewed scope set for this provider")
    result: dict[str, Any] = {"errors": errors, "provedAt": proof.get("provedAt"), "expiresAt": proof.get("expiresAt")}
    if errors:
        return {**result, "state": "invalid"}
    return {**result, "state": "expired" if expires <= now else "valid", "limitations": list(limitations)}


def _entry(root: pathlib.Path, provider: Mapping[str, Any], now: dt.datetime) -> dict[str, Any]:
    adapter = provider.get("adapter")
    adapter_file = root / adapter if isinstance(adapter, str) else None
    tests = adapter_tests(root, provider)
    proof = validate_proof(root, provider, now)
    needs_tenant = provider["tier"] >= 1
    blockers: list[str] = []
    limitations: list[str] = []
    if provider["status"] not in BUILT:
        blockers.append(f"connector is {provider['status']}: no shipped adapter")
    if provider["status"] in BUILT and tests["tests"] == 0:
        blockers.append("no automated adapter tests found")
    if needs_tenant:
        if proof["state"] == "none":
            blockers.append("no retained live tenant proof")
            limitations.append("no live tenant proof is retained")
        elif proof["state"] == "invalid":
            blockers.append("live tenant proof is invalid: " + "; ".join(proof["errors"]))
        elif proof["state"] == "expired":
            blockers.append(f"live tenant proof expired at {proof['expiresAt']}")
        if proof["state"] == "valid" and provider["status"] != "live":
            blockers.append("the registry still says " + provider["status"] + "; update it to live after review")
    else:
        limitations.append("no deployment evidence is retained for this built-in provider")
    if provider["status"] == "live" and needs_tenant and proof["state"] != "valid":
        blockers.append("the registry says live without a valid proof")
    limitations.extend(proof.get("limitations", []))
    if provider["status"] == "in-development":
        limitations.append("adapter is in development and is not served by the governed sidecar")
    entry: dict[str, Any] = {
        "id": provider["id"], "name": provider["name"], "family": provider["family"], "tier": provider["tier"],
        "method": provider["method"], "status": provider["status"], "issue": provider["issue"],
        "tools": sorted(provider["tools"]), "scopes": reviewed_scopes(provider),
        "adapter": {"path": adapter, "sha256": _digest(adapter_file) if adapter_file is not None and adapter_file.is_file() else None},
        "tests": tests,
        "liveProof": {key: proof[key] for key in ("state", "provedAt", "expiresAt") if key in proof},
        "limitations": sorted(set(limitations)),
        "goLive": {"allowed": not blockers, "blockers": blockers},
    }
    return entry


def manifest(root: pathlib.Path = ROOT, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """The evidence manifest for every provider in the registry."""
    root = pathlib.Path(root)
    moment = now or dt.datetime.now(dt.timezone.utc)
    registry = _read_registry(root)
    try:
        release = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        release = None
    providers = [_entry(root, provider, moment) for provider in sorted(registry["providers"], key=lambda item: (item["family"], item["tier"], item["id"]))]
    return {
        "schemaVersion": SCHEMA_VERSION, "kind": "ProviderEvidenceManifest", "generatedAt": moment.isoformat().replace("+00:00", "Z"),
        "registryVersion": registry.get("registryVersion"), "platformRelease": release, "providers": providers,
        "summary": {
            "providers": len(providers), "goLiveAllowed": sum(1 for item in providers if item["goLive"]["allowed"]),
            "liveProofValid": sum(1 for item in providers if item["liveProof"]["state"] == "valid"),
        },
    }


def check(root: pathlib.Path = ROOT, *, now: dt.datetime | None = None) -> list[str]:
    """Structural problems that make the registry's claims untrue now (an expired proof is not one of them)."""
    root = pathlib.Path(root)
    moment = now or dt.datetime.now(dt.timezone.utc)
    registry = _read_registry(root)
    known = {provider["id"] for provider in registry["providers"]}
    errors: list[str] = []
    for provider in registry["providers"]:
        proof = validate_proof(root, provider, moment)
        expected = f"{PROOF_DIR}/{provider['id']}.json"
        if provider["status"] == "live":
            if provider.get("proof") != expected:
                errors.append(f"{provider['id']}: a live provider's proof must be {expected}")
            if proof["state"] == "none":
                errors.append(f"{provider['id']}: status is live but {expected} does not exist")
            elif proof["state"] == "invalid":
                errors.extend(f"{provider['id']}: {error}" for error in proof["errors"])
        elif proof["state"] == "invalid":
            errors.extend(f"{provider['id']}: proof file is malformed: {error}" for error in proof["errors"])
    directory = root / PROOF_DIR
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if path.stem not in known:
                errors.append(f"{PROOF_DIR}/{path.name} names a provider that is not in the registry")
    return errors


def go_live(provider_id: str, root: pathlib.Path = ROOT, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """``goLive`` verdict for one provider; raises ``ValueError`` for an unknown id."""
    for entry in manifest(root, now=now)["providers"]:
        if entry["id"] == provider_id:
            return {"provider": provider_id, **entry["goLive"]}
    raise ValueError(f"unknown provider {provider_id!r}")


def render_markdown(data: Mapping[str, Any]) -> str:
    lines = ["| Provider | Tier | Status | Tests | Live proof | Go-live | Blockers |", "| --- | ---: | --- | ---: | --- | --- | --- |"]
    for item in data["providers"]:
        blockers = "; ".join(item["goLive"]["blockers"]) or "-"
        lines.append(
            f"| {item['name']} (`{item['id']}`) | {item['tier']} | {item['status']} | {item['tests']['tests']} | "
            f"{item['liveProof']['state']} | {'allowed' if item['goLive']['allowed'] else 'blocked'} | {blockers} |"
        )
    return "\n".join(lines) + "\n"
