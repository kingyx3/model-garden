#!/usr/bin/env python3
"""Overlay the optional LiveKit Receptionist voice worker onto one deployed Docker runtime.

The base deployment remains the small Hermes + governed-Tools contract. This script is
only active when EnvironmentBinding.spec.channels.voice.provider == livekit. It switches
Hermes from the local serve process to its authenticated OpenAI-compatible gateway and
adds a LiveKit worker on the same private Compose network. Failure restores the base
runtime, so adding voice does not weaken the existing rollback path.
"""
from __future__ import annotations

import argparse
import hashlib
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
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import client_operation_lock


def _load_deployer():
    path = ROOT / "scripts" / "deploy-client-docker.py"
    spec = importlib.util.spec_from_file_location("model_garden_docker_deployer", path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


DEPLOY = _load_deployer()


def _load_lawful_basis():
    path = ROOT / "platform" / "core" / "lawful_basis.py"
    spec = importlib.util.spec_from_file_location("model_garden_voice_lawful_basis", path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


transcript_environment = _load_lawful_basis().transcript_environment


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("EnvironmentBinding must be a JSON object")
    return value


def _voice(binding: dict[str, Any]) -> dict[str, Any] | None:
    spec = binding.get("spec")
    channels = spec.get("channels") if isinstance(spec, dict) else None
    voice = channels.get("voice") if isinstance(channels, dict) else None
    if voice is None:
        return None
    if not isinstance(voice, dict) or voice.get("provider") != "livekit":
        raise ValueError("channels.voice must use provider: livekit when present")
    required = ("url", "api_key_ref", "api_secret_ref")
    for name in required:
        value = voice.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"channels.voice.{name} must be non-empty")
    if not voice["api_key_ref"].startswith("secret://") or not voice["api_secret_ref"].startswith("secret://"):
        raise ValueError("LiveKit API credentials must be secret:// references")
    target = voice.get("human_transfer_target")
    if target is not None and (not isinstance(target, str) or not (target.startswith("tel:") or target.startswith("sip:"))):
        raise ValueError("channels.voice.human_transfer_target must be tel: or sip: when supplied")
    return voice


def _run(command: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def voice_healthcheck() -> dict[str, Any]:
    """Probe the pinned LiveKit AgentServer's *own* connection/readiness endpoint.

    The server listens on port 8081 in production mode. Probe only container
    loopback; no host port, credentials, caller content or custom health daemon.
    A LiveKit 503 or timeout must produce a non-zero Docker health result.
    """
    return {
        "test": ["CMD", "python", "-c",
                 "import http.client, sys; c=http.client.HTTPConnection('127.0.0.1', 8081, timeout=3); "
                 "c.request('GET', '/'); status=c.getresponse().status; c.close(); "
                 "sys.exit(0 if status == 200 else 1)"],
        "interval": "10s", "timeout": "5s", "retries": 6, "start_period": "30s",
    }


def _compose_ps_rows(raw: str) -> list[dict[str, Any]]:
    """Docker Compose can emit a JSON array or newline-delimited JSON objects."""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        try:
            decoded = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return []
    if isinstance(decoded, dict):
        decoded = [decoded]
    return decoded if isinstance(decoded, list) and decoded and all(isinstance(row, dict) for row in decoded) else []


def _health(project: str, compose_files: list[pathlib.Path], env: dict[str, str], timeout: int = 120,
            *, required_services: dict[str, bool] | None = None) -> bool:
    """All declared Compose services must be present and ready, not just voice.

    Values in required_services indicate whether the merged Compose definition
    declares a healthcheck. An absent optional service is omitted from this map;
    an absent *configured* service must never count as a healthy deployment.
    """
    expected = required_services if required_services is not None else {"hermes": True, "voice": True}
    if not {"hermes", "voice"} <= expected.keys() or not expected["voice"]:
        return False
    command = ["docker", "compose", "-p", project]
    for path in compose_files:
        command.extend(["-f", str(path)])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run([*command, "ps", "--format", "json"], env=env, check=False)
        rows = _compose_ps_rows(result.stdout) if result.returncode == 0 else []
        if rows:
            by_service: dict[str, dict[str, Any]] = {}
            for row in rows:
                name = row.get("Service")
                if not isinstance(name, str) or not name or name in by_service:
                    return False  # malformed/duplicate service identity
                by_service[name] = row
            if any(str(row.get("State") or "").lower() in {"exited", "dead"} or
                   str(row.get("Health") or "").lower() == "unhealthy" for row in rows):
                return False
            ready = True
            for name, has_healthcheck in expected.items():
                row = by_service.get(name)
                if row is None or str(row.get("State") or "").lower() != "running":
                    ready = False
                    break
                health = str(row.get("Health") or "").lower()
                if has_healthcheck and health != "healthy":
                    ready = False
                    break
                if not has_healthcheck and health not in {"", "healthy"}:
                    ready = False
                    break
            if ready:
                return True
        time.sleep(2)
    return False


def _required_services(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, bool]:
    """Merge declared services and healthchecks without trusting Compose ps output."""
    required: dict[str, bool] = {}
    for document in (base, overlay):
        services = document.get("services") if isinstance(document, dict) else None
        if not isinstance(services, dict):
            raise ValueError("voice deployment requires a valid Compose services mapping")
        for name, definition in services.items():
            if not isinstance(name, str) or not name or not isinstance(definition, dict):
                raise ValueError("voice deployment has an invalid Compose service definition")
            required[name] = required.get(name, False) or "healthcheck" in definition
    if "hermes" not in required or "voice" not in required:
        raise ValueError("voice deployment requires Hermes and LiveKit services")
    required["voice"] = True
    return required


def _context(root: pathlib.Path) -> pathlib.Path:
    context = root / "voice-build"
    context.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "requirements-voice.txt", context / "requirements-voice.txt")
    shutil.copy2(ROOT / "platform" / "channels" / "livekit_receptionist.py", context / "livekit_receptionist.py")
    shutil.copy2(ROOT / "platform" / "channels" / "call_transcript.py", context / "call_transcript.py")
    (context / "Dockerfile").write_text(
        """ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nCOPY requirements-voice.txt /tmp/requirements-voice.txt\nRUN python -m pip install --no-cache-dir -r /tmp/requirements-voice.txt\nCOPY livekit_receptionist.py /opt/model-garden-livekit-receptionist.py\nCOPY call_transcript.py /opt/call_transcript.py\nENTRYPOINT [\"python\", \"/opt/model-garden-livekit-receptionist.py\"]\nCMD [\"start\"]\n""",
        encoding="utf-8",
    )
    return context


VOICE_DATA_MOUNT = "/opt/voice"


def worker_overlay(base: dict[str, Any], *, agent_name: str = "model-garden-receptionist") -> dict[str, Any] | None:
    """The governed worker's share of the voice overlay: the voice container's data volume, read-only, and where its transcripts are.

    Transcripts live in the voice container's own volume, which the worker could not see. With this mount the worker files each
    finished call (summary, CRM, timeline) itself; it can read the store and never write to it. ``None`` when the base has no worker.
    """
    if (not isinstance(agent_name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", agent_name) is None):
        raise ValueError("voice agent name must be a valid identifier")
    services = base.get("services") if isinstance(base, dict) else None
    if not isinstance(services, dict) or "governed-worker" not in services:
        return None
    return {
        "environment": {
            "MODEL_GARDEN_VOICE_TRANSCRIPTS": f"{VOICE_DATA_MOUNT}/transcripts.jsonl",
            "MODEL_GARDEN_VOICE_AGENT_NAME": agent_name,
        },
        "volumes": [f"voice-data:{VOICE_DATA_MOUNT}:ro"],
    }


def apply(binding_path: pathlib.Path, bundle_dir: pathlib.Path, project: str, secrets_json: str | None, timeout: int) -> str:
    """Guard the entire voice overlay, health wait and rollback against backup."""
    binding = _load_json(binding_path)
    voice = _voice(binding)
    if voice is None:
        return "voice not selected; base runtime unchanged"
    with client_operation_lock.project_operation_lock(project):
        return _apply_selected_voice(binding, voice, bundle_dir, project, secrets_json, timeout)


def _apply_selected_voice(binding: dict[str, Any], voice: dict[str, Any], bundle_dir: pathlib.Path,
                          project: str, secrets_json: str | None, timeout: int) -> str:
    _, model_ref = DEPLOY._validate_binding(binding)
    resolved = DEPLOY.resolve_secrets(binding, secrets_json)
    model_secret = resolved[model_ref]
    api_key = resolved[voice["api_key_ref"]]
    api_secret = resolved[voice["api_secret_ref"]]

    profile = yaml.safe_load((bundle_dir / "profile" / "config.yaml").read_text(encoding="utf-8"))
    provider = profile.get("model", {}).get("provider") if isinstance(profile, dict) else None
    if provider != "openai":
        raise ValueError("reference LiveKit voice currently requires the OpenAI model credential for STT/TTS")

    internal_key = hashlib.sha256((project + "\0" + model_secret).encode("utf-8")).hexdigest()
    build_root = pathlib.Path(tempfile.mkdtemp(prefix=f"{project}-voice-"))
    context = _context(build_root)
    base = bundle_dir / "compose.yaml"
    overlay = build_root / "compose.voice.yaml"
    agent_name = voice.get("agent_name", "model-garden-receptionist")
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("channels.voice.agent_name must be non-empty when supplied")

    hermes_environment = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "0.0.0.0",
        "API_SERVER_PORT": "8642",
        "API_SERVER_KEY": "${MODEL_GARDEN_HERMES_API_KEY:?MODEL_GARDEN_HERMES_API_KEY is required}",
    }
    voice_environment = {
        "LIVEKIT_URL": voice["url"],
        "LIVEKIT_API_KEY": "${MODEL_GARDEN_LIVEKIT_API_KEY:?MODEL_GARDEN_LIVEKIT_API_KEY is required}",
        "LIVEKIT_API_SECRET": "${MODEL_GARDEN_LIVEKIT_API_SECRET:?MODEL_GARDEN_LIVEKIT_API_SECRET is required}",
        "OPENAI_API_KEY": "${MODEL_GARDEN_MODEL_CREDENTIAL:?MODEL_GARDEN_MODEL_CREDENTIAL is required}",
        "MODEL_GARDEN_HERMES_API_URL": "http://hermes:8642/v1",
        "MODEL_GARDEN_HERMES_API_KEY": "${MODEL_GARDEN_HERMES_API_KEY:?MODEL_GARDEN_HERMES_API_KEY is required}",
        "MODEL_GARDEN_LIVEKIT_AGENT_NAME": agent_name.strip(),
        "MODEL_GARDEN_MESSAGE_PATH": "/opt/data/messages.jsonl",
        "MODEL_GARDEN_TRANSCRIPT_PATH": "/opt/data/transcripts.jsonl",
    }
    if voice.get("human_transfer_target"):
        voice_environment["MODEL_GARDEN_HUMAN_TRANSFER_TARGET"] = voice["human_transfer_target"]
    # Transcript settings come from the binding; turning the spoken disclosure off needs the accountable owner's signature (#107).
    voice_environment.update(transcript_environment(binding, project))

    document = {
        "services": {
            "hermes": {
                "command": ["hermes", "gateway"],
                "environment": hermes_environment,
                "healthcheck": {
                    "test": ["CMD", "python", "-c", "import socket; s=socket.create_connection(('127.0.0.1',8642),5); s.close()"],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 6,
                    "start_period": "20s",
                },
            },
            "voice": {
                "build": {"context": str(context), "args": {"BASE_IMAGE": f"model-garden-{project}:current"}},
                "image": f"model-garden-{project}-voice:current",
                "restart": "unless-stopped",
                "healthcheck": voice_healthcheck(),
                "environment": voice_environment,
                "volumes": ["voice-data:/opt/data"],
                "depends_on": {"hermes": {"condition": "service_healthy"}},
            },
        },
        "volumes": {"voice-data": {}},
    }
    base_document = yaml.safe_load(base.read_text(encoding="utf-8"))
    shared = worker_overlay(base_document, agent_name=agent_name.strip())
    if shared is not None:
        document["services"]["governed-worker"] = shared
    required_services = _required_services(base_document, document)
    overlay.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    runtime_env = os.environ.copy()
    runtime_env.pop("MODEL_GARDEN_RUNTIME_SECRETS_JSON", None)
    runtime_env["MODEL_GARDEN_MODEL_CREDENTIAL"] = model_secret
    runtime_env["MODEL_GARDEN_LIVEKIT_API_KEY"] = api_key
    runtime_env["MODEL_GARDEN_LIVEKIT_API_SECRET"] = api_secret
    runtime_env["MODEL_GARDEN_HERMES_API_KEY"] = internal_key

    compose = ["docker", "compose", "-p", project, "-f", str(base), "-f", str(overlay)]
    try:
        _run([*compose, "up", "-d", "--build", "--remove-orphans"], env=runtime_env)
        if not _health(project, [base, overlay], runtime_env, timeout=timeout,
                       required_services=required_services):
            raise RuntimeError("voice overlay did not become healthy")
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        base_compose = ["docker", "compose", "-p", project, "-f", str(base)]
        _run([*compose, "down"], env=runtime_env, check=False)
        _run([*base_compose, "up", "-d", "--no-build", "--force-recreate"], env=runtime_env, check=False)
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        raise ValueError(f"Voice overlay failed and base runtime restoration was attempted: {detail}") from exc
    finally:
        shutil.rmtree(build_root, ignore_errors=True)
    return f"LiveKit voice worker running as {agent_name.strip()} with Hermes API reasoning and safe human fallback"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment_binding", type=pathlib.Path)
    parser.add_argument("bundle_dir", type=pathlib.Path)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--health-timeout", type=int, default=120)
    args = parser.parse_args()
    try:
        result = apply(
            args.environment_binding,
            args.bundle_dir,
            args.project_name,
            os.environ.get("MODEL_GARDEN_RUNTIME_SECRETS_JSON"),
            args.health_timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"Reference voice deployment failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
