#!/usr/bin/env python3
"""Render one Docker Compose bundle that runs several Agents of a client environment side by side (#95).

  deploy-team-docker.py <profiles-root> <environment-binding.json> <bundle-dir> --project-name acme-dev
  deploy-team-docker.py --profile front-desk=out/front-desk --profile billing=out/billing <binding> <bundle-dir> --project-name acme-dev

This is the opt-in multi-Agent counterpart of `deploy-client-docker.py`, which is untouched: a single-Agent environment keeps using
that script and its bundles are byte-for-byte what they were. Each Agent keeps its own compiled profile (from
`compile-workspace.py --agent NAME` and `provision-hermes-profile.py`), so each gets its own Hermes runtime, governed-tools sidecar,
optional worker / coworker API / gateway, state volumes and private network; nothing is shared between Agents but the model
credential reference and the client environment.

- **Distinct attested identity per Agent.** The sidecar attests an Agent from the `source.name` of the desired state compiled into
  *its* profile, which the Agent's Hermes cannot change. This script requires that name to equal the profile's directory name and
  the name in `EnvironmentBinding.spec.agents`, refuses duplicates, and gives each sidecar its own initiating identity
  `runtime:hermes:<agent>`. Grants, MCP connections, approvals, hand-offs, memory and the hash-chained ledger are all scoped to that
  Agent: ledger events carry its name, and each Agent writes its own ledger on its own volume (export them per Agent).
- **Service names and private networks.** Services are `<agent>-<service>` and each Agent has its own network on which its services
  keep their usual short names (`hermes`, `governed-tools`, `coworker-api`), so the per-Agent profile and worker config work unchanged and
  one Agent's runtime cannot reach another Agent's sidecar.
- **`EnvironmentBinding.spec.agents`** is optional. Either a list of Agent names (checked against the profiles) or an object mapping an
  Agent name to a per-Agent overlay of binding keys (`runtime`, `worker`, `gateway`, `upstream_mcp`, `audit` ...) that replaces the same
  key of the shared binding for that Agent only, for example to run the channel gateway on the front-desk Agent alone.
  Two Agents publishing the same host port is refused.
- Rendering only: no credentials are read or written, and `--apply` is not offered; secrets are injected at apply time exactly as
  for a single Agent.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
MAX_AGENTS = 12
IDENTITY_ENV = "MODEL_GARDEN_INITIATING_IDENTITY"


def _load_deployer():
    spec = importlib.util.spec_from_file_location("model_garden_team_deployer", ROOT / "scripts" / "deploy-client-docker.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


DEPLOYER = _load_deployer()


def agent_identity(agent: str) -> str:
    """The initiating identity of one Agent's sidecar; distinct per Agent and stable across redeploys."""
    return f"runtime:hermes:{agent}"


def _attested_name(profile: pathlib.Path) -> str:
    state = DEPLOYER._desired_state(profile)
    name = state.get("source", {}).get("name") if isinstance(state.get("source"), dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError(f"Team target failed: {profile} has no compiled Agent identity (desired state source.name)")
    return name


def _agent_overlays(binding: dict[str, Any], profiles: dict[str, pathlib.Path]) -> dict[str, dict[str, Any]]:
    spec = binding.get("spec")
    declared = spec.get("agents") if isinstance(spec, dict) else None
    if declared is None:
        return {name: {} for name in profiles}
    if isinstance(declared, list):
        if not all(isinstance(item, str) for item in declared):
            raise ValueError("Team target failed: spec.agents entries must be Agent names")
        overlays = {item: {} for item in declared}
        if len(overlays) != len(declared):
            raise ValueError("Team target failed: spec.agents lists an Agent twice")
    elif isinstance(declared, dict):
        if not all(isinstance(value, dict) for value in declared.values()):
            raise ValueError("Team target failed: each spec.agents entry must be an object of per-Agent binding keys")
        overlays = {key: dict(value) for key, value in declared.items()}
    else:
        raise ValueError("Team target failed: spec.agents must be a list of names or an object of per-Agent overlays")
    if set(overlays) != set(profiles):
        raise ValueError(
            "Team target failed: EnvironmentBinding spec.agents "
            f"({', '.join(sorted(overlays))}) does not match the profiles ({', '.join(sorted(profiles))})"
        )
    for name, overlay in overlays.items():
        if "agents" in overlay or "environment" in overlay:
            raise ValueError(f"Team target failed: Agent {name!r} overlay may not change agents or environment")
    return overlays


def _agent_binding(binding: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(binding)
    spec = result["spec"]
    spec.pop("agents", None)
    spec.update(copy.deepcopy(overlay))
    return result


def _published(service: dict[str, Any]) -> list[str]:
    ports = []
    for port in service.get("ports", []) or []:
        parts = str(port).split(":")
        ports.append(":".join(parts[:-1]) if len(parts) > 1 else parts[0])
    return ports


def _merge_compose(composes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    networks: dict[str, Any] = {}
    claimed: dict[str, str] = {}
    for agent, compose in composes.items():
        networks[agent] = {}
        for short, service in compose["services"].items():
            merged = copy.deepcopy(service)
            if "build" in merged:
                merged["build"]["context"] = f"./agents/{agent}"
            merged["networks"] = {agent: {"aliases": [short]}}
            if "depends_on" in merged:
                merged["depends_on"] = {f"{agent}-{name}": value for name, value in merged["depends_on"].items()}
            merged["volumes"] = [f"{agent}-{item}" for item in merged.get("volumes", [])]
            if short == "governed-tools":
                environment = merged.setdefault("environment", {})
                environment[IDENTITY_ENV] = agent_identity(agent)
                environment["MODEL_GARDEN_AGENT"] = agent
            for published in _published(merged):
                if published in claimed:
                    raise ValueError(
                        f"Team target failed: Agents {claimed[published]!r} and {agent!r} both publish host port {published}; "
                        "enable the gateway for one Agent with a spec.agents overlay"
                    )
                claimed[published] = agent
            services[f"{agent}-{short}"] = merged
        for name, value in compose.get("volumes", {}).items():
            volumes[f"{agent}-{name}"] = value
    return {"services": services, "volumes": volumes, "networks": networks}


def render_team_bundle(
    profiles: dict[str, pathlib.Path],
    binding_path: pathlib.Path,
    bundle_dir: pathlib.Path,
    project_name: str,
    *,
    platform_root: pathlib.Path = ROOT,
    fetch_source: bool = False,
) -> dict[str, Any]:
    if not DEPLOYER.PROJECT_NAME.fullmatch(project_name):
        raise ValueError("Team target failed: project name must be lowercase letters, numbers, and internal hyphens")
    if not 2 <= len(profiles) <= MAX_AGENTS:
        raise ValueError(f"Team target failed: a team is 2 to {MAX_AGENTS} Agents; use deploy-client-docker.py for one")
    for name, profile in profiles.items():
        if not AGENT_NAME.fullmatch(name):
            raise ValueError(f"Team target failed: Agent name {name!r} must be lowercase letters, numbers and internal hyphens (31 characters at most)")
        if _attested_name(profile) != name:
            raise ValueError(f"Team target failed: profile {name!r} compiles Agent {_attested_name(profile)!r}; the names must match")
    if len(project_name) + 1 + max(len(name) for name in profiles) > 62:
        raise ValueError("Team target failed: project name plus Agent name is too long")
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise ValueError(f"Team target failed: refusing to overwrite non-empty bundle directory {bundle_dir}")
    binding = DEPLOYER._load_json(binding_path, "EnvironmentBinding")
    environment, credential_ref = DEPLOYER._validate_binding(binding)
    overlays = _agent_overlays(binding, profiles)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    composes: dict[str, dict[str, Any]] = {}
    agents: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        for name in sorted(profiles):
            per_agent = pathlib.Path(scratch) / f"{name}.json"
            per_agent.write_text(json.dumps(_agent_binding(binding, overlays[name])), encoding="utf-8")
            metadata = DEPLOYER.render_bundle(
                profiles[name], per_agent, bundle_dir / "agents" / name, f"{project_name}-{name}",
                platform_root=platform_root, fetch_source=fetch_source,
            )
            composes[name] = yaml.safe_load((bundle_dir / "agents" / name / "compose.yaml").read_text(encoding="utf-8"))
            agents.append({
                "agent": name, "identity": agent_identity(name), "network": name,
                "services": sorted(f"{name}-{short}" for short in composes[name]["services"]),
                "selectedTools": metadata["selectedTools"], "governedToolService": metadata["governedToolService"],
                "channelGateway": metadata["channelGateway"], "bindingSha256": metadata["bindingSha256"],
                "capabilityManifestSha256": metadata["capabilityManifestSha256"],
            })
    compose_text = yaml.safe_dump(_merge_compose(composes), sort_keys=False)
    (bundle_dir / "compose.yaml").write_text(compose_text, encoding="utf-8")
    normalized = json.dumps(binding, indent=2, sort_keys=True) + "\n"
    (bundle_dir / "environment-binding.json").write_text(normalized, encoding="utf-8")
    metadata = {
        "apiVersion": "modelgarden.ai/v1", "kind": "DockerTeamDeploymentBundle", "project": project_name, "environment": environment,
        "modelCredentialRef": credential_ref, "agents": agents, "bindingSha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "composeSha256": hashlib.sha256(compose_text.encode("utf-8")).hexdigest(),
    }
    (bundle_dir / "deployment-metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=pathlib.Path, help="[profiles-root] environment-binding.json bundle-dir")
    parser.add_argument("--profile", action="append", default=[], metavar="NAME=DIR", help="an Agent profile (repeatable) instead of a profiles-root")
    parser.add_argument("--project-name", required=True, help="stable lowercase client-environment deployment name")
    args = parser.parse_args()
    try:
        if args.profile:
            if len(args.paths) != 2:
                raise ValueError("Team target failed: with --profile give the binding and the bundle directory")
            profiles = {}
            for item in args.profile:
                name, _, directory = item.partition("=")
                if not name or not directory or name in profiles:
                    raise ValueError(f"Team target failed: --profile expects a unique NAME=DIR, got {item!r}")
                profiles[name] = pathlib.Path(directory)
            binding_path, bundle_dir = args.paths
        else:
            if len(args.paths) != 3:
                raise ValueError("Team target failed: give a profiles root, the binding and the bundle directory")
            root, binding_path, bundle_dir = args.paths
            profiles = {child.name: child for child in sorted(root.iterdir()) if child.is_dir()}
        metadata = render_team_bundle(profiles, binding_path, bundle_dir, args.project_name)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Rendered Docker team target {metadata['project']} ({metadata['environment']}) with {len(metadata['agents'])} Agents: "
          + ", ".join(item["agent"] for item in metadata["agents"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
