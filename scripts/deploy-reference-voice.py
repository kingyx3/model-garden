#!/usr/bin/env python3
"""Overlay the optional LiveKit Receptionist voice worker onto one deployed Docker runtime.

The base deployment remains the small Hermes + governed-Tools contract. This script is
only active when EnvironmentBinding.spec.channels.voice.provider == livekit. It switches
Hermes from the local serve process to its authenticated OpenAI-compatible gateway and
adds a LiveKit worker on the runtime-facing Compose network. Failure restores the base
runtime, so adding voice does not weaken the existing rollback path.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_deployer():
    path = ROOT / "scripts" / "deploy-client-docker.py"
    spec = importlib.util.spec_from_file_location("model_garden_docker_deployer", path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


DEPLOY = _load_deployer()


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


def _health(project: str, compose_files: list[pathlib.Path], env: dict[str, str], timeout: int = 120) -> bool:
    command = ["docker", "compose", "-p", project]
    for path in compose_files:
        command.extend(["-f", str(path)])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run([*command, "ps", "--format", "json"], env=env, check=False)
        if result.returncode == 0 and result.stdout.strip():
            rows = []
            for line in result.stdout.splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows = []
                    break
            long_running = [row for row in rows if row.get("Service") != "voice-volume-init"]
            if long_running and any(row.get("Service") == "voice" for row in long_running):
                bad = {"exited", "dead", "unhealthy"}
                states = {(row.get("State") or "").lower() for row in long_running}
                health = {(row.get("Health") or "").lower() for row in long_running if row.get("Health")}
                if states & bad or health & bad:
                    return False
                hermes_ok = any(row.get("Service") == "hermes" and (row.get("Health") == "healthy" or row.get("State") == "running") for row in long_running)
                voice_ok = any(row.get("Service") == "voice" and row.get("State") == "running" for row in long_running)
                tools_ok = all(row.get("Service") != "governed-tools" or row.get("Health") == "healthy" for row in long_running)
                if hermes_ok and voice_ok and tools_ok:
                    return True
        time.sleep(2)
    return False


def _context(root: pathlib.Path) -> pathlib.Path:
    context = root / "voice-build"
    context.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "requirements-voice.txt", context / "requirements-voice.txt")
    shutil.copy2(ROOT / "platform" / "channels" / "livekit_receptionist.py", context / "livekit_receptionist.py")
    (context / "Dockerfile").write_text(
        """ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nCOPY requirements-voice.txt /tmp/requirements-voice.txt\nRUN python -m pip install --no-cache-dir -r /tmp/requirements-voice.txt\nCOPY livekit_receptionist.py /opt/model-garden-livekit-receptionist.py\nENTRYPOINT [\"python\", \"/opt/model-garden-livekit-receptionist.py\"]\nCMD [\"start\"]\n""",
        encoding="utf-8",
    )
    return context


def _voice_volume_init(project: str) -> dict[str, Any]:
    return {
        "image": f"model-garden-{project}:current",
        "user": "0:0",
        "entrypoint": ["/bin/sh", "-c"],
        "command": f"chown -R {DEPLOY.RUNTIME_UID}:{DEPLOY.RUNTIME_GID} /voice",
        "volumes": ["voice-data:/voice"],
        "network_mode": "none",
        "restart": "no",
        "read_only": True,
        "cap_drop": ["ALL"],
        "cap_add": ["CHOWN"],
        "security_opt": ["no-new-privileges:true"],
    }


def apply(binding_path: pathlib.Path, bundle_dir: pathlib.Path, project: str, secrets_json: str | None, timeout: int) -> str:
    binding = _load_json(binding_path)
    voice = _voice(binding)
    if voice is None:
        return "voice not selected; base runtime unchanged"
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
        "HOME": "/tmp",
        "XDG_CACHE_HOME": "/tmp/.cache",
        "LIVEKIT_URL": voice["url"],
        "LIVEKIT_API_KEY": "${MODEL_GARDEN_LIVEKIT_API_KEY:?MODEL_GARDEN_LIVEKIT_API_KEY is required}",
        "LIVEKIT_API_SECRET": "${MODEL_GARDEN_LIVEKIT_API_SECRET:?MODEL_GARDEN_LIVEKIT_API_SECRET is required}",
        "OPENAI_API_KEY": "${MODEL_GARDEN_MODEL_CREDENTIAL:?MODEL_GARDEN_MODEL_CREDENTIAL is required}",
        "MODEL_GARDEN_HERMES_API_URL": "http://hermes:8642/v1",
        "MODEL_GARDEN_HERMES_API_KEY": "${MODEL_GARDEN_HERMES_API_KEY:?MODEL_GARDEN_HERMES_API_KEY is required}",
        "MODEL_GARDEN_LIVEKIT_AGENT_NAME": agent_name.strip(),
        "MODEL_GARDEN_MESSAGE_PATH": "/opt/data/messages.jsonl",
    }
    if voice.get("human_transfer_target"):
        voice_environment["MODEL_GARDEN_HUMAN_TRANSFER_TARGET"] = voice["human_transfer_target"]

    voice_service: dict[str, Any] = {
        "build": {"context": str(context), "args": {"BASE_IMAGE": f"model-garden-{project}:current"}},
        "image": f"model-garden-{project}-voice:current",
        "restart": "unless-stopped",
        "environment": voice_environment,
        "volumes": ["voice-data:/opt/data"],
        "networks": ["default"],
        "depends_on": {
            "hermes": {"condition": "service_healthy"},
            "voice-volume-init": {"condition": "service_completed_successfully"},
        },
        **DEPLOY._container_hardening(),
    }
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
            "voice-volume-init": _voice_volume_init(project),
            "voice": voice_service,
        },
        "volumes": {"voice-data": {}},
    }
    overlay.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    runtime_env = os.environ.copy()
    runtime_env.pop("MODEL_GARDEN_RUNTIME_SECRETS_JSON", None)
    runtime_env["MODEL_GARDEN_MODEL_CREDENTIAL"] = model_secret
    runtime_env["MODEL_GARDEN_LIVEKIT_API_KEY"] = api_key
    runtime_env["MODEL_GARDEN_LIVEKIT_API_SECRET"] = api_secret
    runtime_env["MODEL_GARDEN_HERMES_API_KEY"] = internal_key
    spec = binding.get("spec", {})
    connectors = spec.get("connectors", {}) if isinstance(spec, dict) else {}
    calendar = connectors.get("google_calendar") if isinstance(connectors, dict) else None
    if isinstance(calendar, dict):
        ref = calendar.get("token_ref")
        if isinstance(ref, str) and ref in resolved:
            runtime_env["MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN"] = resolved[ref]

    compose = ["docker", "compose", "-p", project, "-f", str(base), "-f", str(overlay)]
    try:
        _run([*compose, "up", "-d", "--build", "--remove-orphans"], env=runtime_env)
        if not _health(project, [base, overlay], runtime_env, timeout=timeout):
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
