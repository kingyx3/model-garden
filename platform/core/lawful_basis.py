"""An owner-signed lawful-basis override for running call transcription without the spoken disclosure (#107).

A transcript is only allowed without the "this call is transcribed" notice when an accountable owner has recorded a lawful basis for
it. Until now that was one environment variable naming an owner (``MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER``): anyone who
could edit the deployment could type a name. This module makes the override an *attestation*, the same way a relaxing Policy rule is
(``policy.py``, #114): the owner signs it with their Ed25519 key, the matching public key is in the EnvironmentBinding's
``governance.ownerKeys``, and the deploy refuses to start the voice worker without disclosure unless the signature verifies. It is
pure and credential-free; the private key never enters the workspace or the deployment, and no call is made over a network.

- **What is signed.** The purpose, the owner, the stated basis (free text, at most 300 characters) and the client-environment the
  override is for (the Compose project name). Changing any of them, or reusing the attestation for another client or environment,
  invalidates it.
- **Only the listed owner's key counts.** The attestation's owner must be an identity in ``governance.ownerKeys`` and the signature
  must verify with *that* owner's key. With no owner keys configured the override cannot be attested and disclosure cannot be turned off
  through the binding.
- **Verified at deploy, trusted by the worker.** ``transcript_environment`` turns the binding's ``channels.voice.transcript`` block
  into the worker's environment and re-validates the result with the worker's own parser, so an unsafe combination fails at deploy
  rather than on a live call. The worker (a small image with no signing library) still reads the verified owner from its
  environment and records it on every transcript, as before; what changed is that the value can no longer be set without the signature.
- **Not done here.** It proves the named owner signed, not that the basis is legally sufficient, and it does not cover a deployment
  that edits the worker's environment directly after deploy.
"""
from __future__ import annotations

import base64
import binascii
import importlib.util
import json
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Any

PURPOSE = "transcript-disclosure-off"
SIGNATURE_CONTEXT = b"model-garden-lawful-basis/1\n"
MAX_BASIS = 300
TRANSCRIPT_KEYS = {"mode", "retention_seconds", "disclosure", "redact_patterns", "lawful_basis_override"}
OVERRIDE_KEYS = {"owner", "basis", "signature"}
_PROJECT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _module(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parents[1] / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _policy():
    return _module("model_garden_lawful_basis_policy", "core/policy.py")


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > limit:
        raise ValueError(f"{label} is at most {limit} characters")
    return cleaned


def _message(owner: str, basis: str, project: str) -> bytes:
    body = json.dumps({"purpose": PURPOSE, "owner": owner.strip().lower(), "basis": basis, "project": project},
                      sort_keys=True, separators=(",", ":"))
    return SIGNATURE_CONTEXT + body.encode("utf-8")


def sign_override(owner: str, basis: str, project: str, private_key: str) -> dict[str, str]:
    """The owner's attestation for ``project``: their identity, the basis and an Ed25519 signature over both and the project."""
    owner = _text(owner, "owner", 128)
    basis = _text(basis, "basis", MAX_BASIS)
    if not isinstance(project, str) or not _PROJECT.match(project):
        raise ValueError("project must be the client-environment slug used as the Compose project name")
    try:
        key = _policy()._ed25519().Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key, validate=True))
    except (ValueError, binascii.Error):
        raise ValueError("the owner private key must be a base64 Ed25519 key") from None
    return {"owner": owner, "basis": basis, "signature": base64.b64encode(key.sign(_message(owner, basis, project))).decode()}


def verify_override(attestation: Any, owner_keys: Mapping[str, str] | None, *, project: str) -> str:
    """The owner's identity when the attestation is genuine for ``project``; otherwise ``ValueError`` naming what is wrong (never the key)."""
    if not isinstance(attestation, Mapping) or set(attestation) - OVERRIDE_KEYS or not OVERRIDE_KEYS <= set(attestation):
        raise ValueError("lawful_basis_override holds exactly owner, basis and signature")
    owner = _text(attestation["owner"], "lawful_basis_override.owner", 128)
    basis = _text(attestation["basis"], "lawful_basis_override.basis", MAX_BASIS)
    if not owner_keys:
        raise ValueError("turning the spoken disclosure off needs governance.ownerKeys, so the lawful basis can be signed by its owner")
    public = owner_keys.get(owner.strip().lower())
    if public is None:
        raise ValueError(f"lawful_basis_override.owner {owner!r} is not listed in governance.ownerKeys")
    try:
        key = _policy()._ed25519().Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))
        key.verify(base64.b64decode(str(attestation["signature"]), validate=True), _message(owner, basis, project))
    except Exception:  # noqa: BLE001 - any failure to verify is a refusal
        raise ValueError("lawful_basis_override is not signed by that owner for this client environment and basis") from None
    return owner.strip()


def transcript_environment(binding: Mapping[str, Any], *, project: str) -> dict[str, str]:
    """The voice worker's ``MODEL_GARDEN_TRANSCRIPT*`` environment from ``spec.channels.voice.transcript``; ``{}`` when it is absent.

    Raises ``ValueError`` for an unknown key, a bad value, a disclosure turned off without a verified attestation, or any combination
    the worker's own parser would refuse at start-up.
    """
    spec = binding.get("spec") if isinstance(binding, Mapping) else None
    channels = spec.get("channels") if isinstance(spec, Mapping) else None
    voice = channels.get("voice") if isinstance(channels, Mapping) else None
    block = voice.get("transcript") if isinstance(voice, Mapping) else None
    if block is None:
        return {}
    if not isinstance(block, Mapping) or set(block) - TRANSCRIPT_KEYS:
        raise ValueError("channels.voice.transcript holds only: " + ", ".join(sorted(TRANSCRIPT_KEYS)))
    environment: dict[str, str] = {}
    if "mode" in block:
        if block["mode"] not in ("on", "off"):
            raise ValueError("channels.voice.transcript.mode must be on or off")
        environment["MODEL_GARDEN_TRANSCRIPT"] = block["mode"]
    if "retention_seconds" in block:
        value = block["retention_seconds"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("channels.voice.transcript.retention_seconds must be an integer")
        environment["MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS"] = str(value)
    if "redact_patterns" in block:
        patterns = block["redact_patterns"]
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise ValueError("channels.voice.transcript.redact_patterns must be a list of regular expressions")
        environment["MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS"] = json.dumps(patterns)
    disclosure_off = False
    if "disclosure" in block:
        disclosure = block["disclosure"]
        if not isinstance(disclosure, str) or not disclosure.strip():
            raise ValueError("channels.voice.transcript.disclosure is the spoken notice, or off")
        disclosure_off = disclosure.strip().lower() == "off"
        environment["MODEL_GARDEN_TRANSCRIPT_DISCLOSURE"] = "off" if disclosure_off else disclosure.strip()
    override = block.get("lawful_basis_override")
    transcribing = environment.get("MODEL_GARDEN_TRANSCRIPT", "on") == "on"
    if disclosure_off and transcribing:
        if override is None:
            raise ValueError("channels.voice.transcript.disclosure: off needs a signed lawful_basis_override from the accountable owner")
        owner_keys = _policy().owner_keys_from_binding(binding)
        owner = verify_override(override, owner_keys, project=project)
        environment["MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER"] = owner
    elif override is not None:
        raise ValueError("channels.voice.transcript.lawful_basis_override is only meaningful with disclosure: off and transcription on")
    # Fail here, not on a live call, for anything the worker itself would refuse at start-up.
    try:
        _module("model_garden_lawful_basis_transcript", "channels/call_transcript.py").config_from_env(environment)
    except RuntimeError as exc:
        raise ValueError(f"channels.voice.transcript is unsafe: {exc}") from None
    return environment
