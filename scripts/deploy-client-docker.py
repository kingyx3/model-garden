#!/usr/bin/env python3
"""Render and optionally deploy one isolated client runtime with Docker Compose.

The Docker target keeps the MVP production shape deliberately small:
- Hermes/model execution runs in one container with only the selected model credential;
- supported enterprise Tools run in a separate governed MCP sidecar with only their
  connector credentials;
- both consume the same compiled desired state; persistent audit/approval state is
  mounted only into the governed sidecar so the Agent runtime cannot manufacture authority;
- raw credentials never enter the generated profile, binding, bundle, or Git repository.

GitHub Actions remains the engineering control plane. The target host is only the
client-scoped runtime/deployment executor.
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
import time
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
BASE_IMAGE = "python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
CALENDAR_TOOLS = {"calendar.availability", "calendar.book"}
# Tier 0 tools served by the sidecar from compiled desired state; they need no provider credential.
BUILT_IN_TOOLS = {"kb.search", "crm.read", "crm.create", "crm.log-interaction", "docs.create",
                  "docs.read", "files.search", "files.read", "files.write",
                  "tickets.search", "tickets.create", "tickets.update"}
RUNTIME_SOURCE_PATHS = (
    pathlib.Path("requirements-runtime.txt"),
    pathlib.Path("platform/runtime/governed_mcp.py"),
    pathlib.Path("platform/runtime/worker.py"),
    pathlib.Path("platform/runtime/coworker_client.py"),
    pathlib.Path("platform/metering/usage.py"),
    pathlib.Path("platform/runtime/gateway.py"),
    pathlib.Path("platform/runtime/upstream_http.py"),
    pathlib.Path("platform/channels/ingress.py"),
    pathlib.Path("platform/channels/receipts.py"),
    pathlib.Path("platform/channels/transports.py"),
    pathlib.Path("platform/channels/smtp_intake.py"),
    pathlib.Path("platform/core/schedule_commands.py"),
    pathlib.Path("platform/core/capabilities.py"),
    pathlib.Path("platform/core/policy.py"),
    pathlib.Path("platform/core/policy_deploy.py"),
    pathlib.Path("platform/core/handoff.py"),
    pathlib.Path("platform/core/handoff_reply.py"),
    pathlib.Path("platform/core/knowledge.py"),
    pathlib.Path("platform/core/contacts.py"),
    pathlib.Path("platform/core/inbox.py"),
    pathlib.Path("platform/core/memory.py"),
    pathlib.Path("platform/core/delegation.py"),
    pathlib.Path("platform/core/schedules.py"),
    pathlib.Path("platform/core/scheduler.py"),
    pathlib.Path("platform/core/human_step.py"),
    pathlib.Path("platform/core/crm_sync.py"),
    pathlib.Path("platform/core/event_outbox.py"),
    pathlib.Path("platform/core/artifacts.py"),
    pathlib.Path("platform/core/builtin_files.py"),
    pathlib.Path("platform/core/tickets.py"),
    pathlib.Path("platform/core/redaction.py"),
    pathlib.Path("platform/core/ledger_export.py"),
    pathlib.Path("platform/core/export_monitor.py"),
    pathlib.Path("platform/core/connection_notices.py"),
    pathlib.Path("platform/core/notice_delivery.py"),
    pathlib.Path("platform/core/crm_filing.py"),
    pathlib.Path("platform/core/connect.py"),
    pathlib.Path("platform/core/deliverables.py"),
    pathlib.Path("platform/core/skillsets.py"),
    pathlib.Path("platform/core/provider_grants.py"),
    pathlib.Path("platform/core/grant_readiness.py"),
    pathlib.Path("platform/core/secret_enrollment.py"),
    pathlib.Path("platform/core/sealed_vault.py"),
    pathlib.Path("platform/core/oauth_consent.py"),
    pathlib.Path("platform/core/knowledge_deploy.py"),
    pathlib.Path("platform/core/team.py"),
    pathlib.Path("platform/core/mcp_passthrough.py"),
    pathlib.Path("platform/core/mcp_executor.py"),
    pathlib.Path("platform/core/mcp_connections.py"),
    pathlib.Path("platform/core/endpoint_guard.py"),
    pathlib.Path("platform/core/mcp_tool_review.py"),
    pathlib.Path("platform/core/custom_api.py"),
    pathlib.Path("platform/channels/text_channel.py"),
    pathlib.Path("platform/channels/email_channel.py"),
    pathlib.Path("platform/channels/approval_confirmation.py"),
    pathlib.Path("platform/channels/slack_channel.py"),
    pathlib.Path("platform/channels/teams_channel.py"),
    pathlib.Path("platform/channels/web_chat.py"),
    pathlib.Path("platform/channels/conversation_filing.py"),
    pathlib.Path("platform/channels/conversation_recorder.py"),
    pathlib.Path("platform/core/audit_query.py"),
    pathlib.Path("platform/channels/call_summary.py"),
    pathlib.Path("platform/channels/call_requests.py"),
    pathlib.Path("platform/channels/call_transcript.py"),
    pathlib.Path("platform/core/conversations.py"),
    pathlib.Path("platform/channels/support_mailbox.py"),
    pathlib.Path("platform/channels/mail_spool.py"),
    pathlib.Path("contracts/v1/capabilities.json"),
    pathlib.Path("contracts/v1/integrations.json"),
    pathlib.Path("scripts/govern-action.py"),
    pathlib.Path("scripts/runtime-approval.py"),
    pathlib.Path("scripts/runtime-handoff.py"),
    pathlib.Path("scripts/runtime-schedules.py"),
    pathlib.Path("scripts/runtime-mailbox.py"),
    pathlib.Path("scripts/runtime-contacts.py"),
    pathlib.Path("scripts/runtime-file-calls.py"),
    pathlib.Path("scripts/runtime-inbox.py"),
    pathlib.Path("scripts/runtime-memory.py"),
    pathlib.Path("scripts/runtime-artifacts.py"),
    pathlib.Path("scripts/runtime-conversations.py"),
    pathlib.Path("scripts/runtime-crm-sync.py"),
    pathlib.Path("scripts/runtime-connection.py"),
    pathlib.Path("scripts/runtime-mcp-connections.py"),
    pathlib.Path("VERSION"),
)
MANIFEST_PATH = pathlib.Path(".modelgarden") / "capability-manifest.json"
WORKER_CONFIG_PATH = pathlib.Path(".modelgarden") / "worker.json"
COWORKER_API_PORT = 8642
READ_AUDIT = re.compile(r"^(?:full|aggregate|sampled:[1-9][0-9]{0,5})$")


def _load_capabilities():
    path = ROOT / "platform" / "core" / "capabilities.py"
    module_spec = importlib.util.spec_from_file_location("model_garden_deploy_capabilities", path)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


CAPABILITIES = _load_capabilities()


def _load_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _load_hermes_lock(platform_root: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (platform_root / "platform" / "hermes.lock").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    for key in ("repository", "commit", "version"):
        if not values.get(key):
            raise ValueError(f"Docker target failed: Hermes lock is missing {key}")
    return values


def _validate_profile(profile_dir: pathlib.Path) -> str:
    required = (
        profile_dir / "SOUL.md",
        profile_dir / "config.yaml",
        profile_dir / ".modelgarden" / "desired-state.json",
    )
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Docker target failed: missing or unsafe generated profile file {path}")
    for path in profile_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Docker target failed: generated profile may not contain symlinks: {path}")
    config = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Docker target failed: Hermes config.yaml must be a mapping")
    model = config.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("provider"), str) or not model["provider"]:
        raise ValueError("Docker target failed: Hermes config.yaml has no model.provider")
    provider = model["provider"].strip()
    if provider not in PROVIDER_ENV:
        raise ValueError(
            f"Docker target failed: model provider {provider!r} has no approved MVP credential mapping"
        )
    return provider


def _desired_state(profile_dir: pathlib.Path) -> dict[str, Any]:
    return _load_json(profile_dir / ".modelgarden" / "desired-state.json", "compiled desired state")


def _capability_manifest(desired: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on unbound required capabilities, then build the non-secret manifest."""
    declared = desired.get("capabilities")
    agent = desired.get("source", {}).get("name", "<unknown>")
    try:
        if isinstance(declared, dict) and (declared.get("required") or declared.get("optional")):
            CAPABILITIES.resolve(
                {"required": declared.get("required", []), "optional": declared.get("optional", [])},
                binding,
                agent_name=agent,
                required_by=CAPABILITIES.skills_requiring(
                    [skill for skill in desired.get("skills", []) if isinstance(skill, dict)]
                ),
            )
        return CAPABILITIES.build_manifest(desired, binding)
    except ValueError as exc:
        raise ValueError(f"Docker target failed: {exc}") from exc


def _read_audit_mode(binding: dict[str, Any]) -> str | None:
    spec = binding.get("spec")
    audit = spec.get("audit") if isinstance(spec, dict) else None
    if audit is None:
        return None
    if not isinstance(audit, dict):
        raise ValueError("Docker target failed: EnvironmentBinding audit must be an object")
    mode = audit.get("reads")
    if mode is None:
        return None
    if not isinstance(mode, str) or not READ_AUDIT.fullmatch(mode):
        raise ValueError("Docker target failed: audit.reads must be full, aggregate or sampled:<N>")
    return mode


def _approval_ttl_hours(binding: dict[str, Any]) -> str | None:
    """``spec.approvals.ttl_hours``: how long an approval request or decision stays valid (1 to 720; unset keeps the runtime default of 24)."""
    spec = binding.get("spec")
    approvals = spec.get("approvals") if isinstance(spec, dict) else None
    if approvals is None:
        return None
    if not isinstance(approvals, dict) or set(approvals) - {"ttl_hours"}:
        raise ValueError("Docker target failed: EnvironmentBinding approvals holds only ttl_hours")
    hours = approvals.get("ttl_hours")
    if hours is None:
        return None
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 720:
        raise ValueError("Docker target failed: approvals.ttl_hours must be a whole number of hours from 1 to 720")
    return str(hours)


def _selected_tools(profile_dir: pathlib.Path) -> set[str]:
    desired = _desired_state(profile_dir)
    tools = desired.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("Docker target failed: compiled desired state tools must be an array")
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Docker target failed: compiled Tool must be an object")
        name = tool.get("metadata", {}).get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Docker target failed: compiled Tool metadata.name must be non-empty")
        names.add(name)
    return names


def _validate_binding(binding: dict[str, Any]) -> tuple[str, str]:
    if binding.get("apiVersion") != "modelgarden.ai/v1" or binding.get("kind") != "EnvironmentBinding":
        raise ValueError("Docker target failed: expected modelgarden.ai/v1 EnvironmentBinding")
    spec = binding.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("Docker target failed: EnvironmentBinding.spec must be an object")
    environment = spec.get("environment")
    if not isinstance(environment, str) or not environment:
        raise ValueError("Docker target failed: EnvironmentBinding has no environment")
    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("Docker target failed: EnvironmentBinding has no runtime object")
    credential_ref = runtime.get("model_credential_ref")
    if not isinstance(credential_ref, str) or not credential_ref.startswith("secret://") or len(credential_ref) <= len("secret://"):
        raise ValueError(
            "Docker target failed: runtime.model_credential_ref must be a non-empty secret:// reference"
        )
    return environment, credential_ref


def _collect_secret_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            refs.update(_collect_secret_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_collect_secret_refs(child))
    elif isinstance(value, str) and value.startswith("secret://"):
        refs.add(value)
    return refs


def resolve_secrets(binding: dict[str, Any], encoded: str | None) -> dict[str, str]:
    """Resolve exactly the references present in this binding, rejecting over-broad input."""
    refs = _collect_secret_refs(binding)
    if not refs:
        return {}
    if not encoded:
        raise ValueError("Docker target failed: MODEL_GARDEN_RUNTIME_SECRETS_JSON is required")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("Docker target failed: runtime secrets JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("Docker target failed: runtime secrets JSON must be an object")
    supplied = set(value)
    missing = sorted(refs - supplied)
    extra = sorted(supplied - refs)
    if missing:
        raise ValueError(f"Docker target failed: unresolved secret references: {', '.join(missing)}")
    if extra:
        raise ValueError(f"Docker target failed: runtime secret map contains unreferenced keys: {', '.join(extra)}")
    resolved: dict[str, str] = {}
    for ref in sorted(refs):
        secret = value.get(ref)
        if not isinstance(secret, str) or not secret:
            raise ValueError(f"Docker target failed: {ref} must resolve to a non-empty string")
        resolved[ref] = secret
    return resolved


def _worker_settings(binding: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """``EnvironmentBinding.spec.worker`` as the non-secret worker configuration, plus the coworker API key reference.

    The worker (``platform/runtime/worker.py``) runs schedules, drains the built-in inbound mailbox and delivers notices. It is
    opt-in: without ``worker.enabled: true`` the bundle is unchanged. The only secret is the Hermes API server key, given as a
    ``secret://`` reference and injected at apply time; everything rendered into the bundle is non-secret.
    """
    spec = binding.get("spec", {})
    worker = spec.get("worker")
    if worker is None:
        return None, None
    if not isinstance(worker, dict):
        raise ValueError("Docker target failed: EnvironmentBinding worker must be an object")
    if worker.get("enabled") is not True:
        return None, None
    allowed = {"enabled", "coworker_api_key_ref", "people", "escalate_to", "owners", "mailbox", "intervals", "artifact_retention_days", "ledger_export"}
    unknown = sorted(set(worker) - allowed)
    if unknown:
        raise ValueError(f"Docker target failed: unknown worker settings: {', '.join(unknown)}")
    key_ref = worker.get("coworker_api_key_ref")
    if key_ref is not None and (not isinstance(key_ref, str) or not key_ref.startswith("secret://") or len(key_ref) <= len("secret://")):
        raise ValueError("Docker target failed: worker.coworker_api_key_ref must be a secret:// reference")
    config: dict[str, Any] = {
        "people": worker.get("people") or {},
        "owners": worker.get("owners") or {},
        "connectors": spec.get("connectors") or {},
    }
    if worker.get("escalate_to"):
        config["escalateTo"] = worker["escalate_to"]
    if worker.get("intervals"):
        config["intervals"] = worker["intervals"]
    if worker.get("artifact_retention_days") is not None:
        config["artifactRetentionDays"] = worker["artifact_retention_days"]
    export = worker.get("ledger_export")
    if export is not None:
        if not isinstance(export, dict) or set(export) - {"gcs_bucket", "impersonate_service_account", "prefix"} or not export.get("gcs_bucket"):
            raise ValueError("Docker target failed: worker.ledger_export needs gcs_bucket and may add impersonate_service_account and prefix")
        config["export"] = {"gcsBucket": export["gcs_bucket"]}
        if export.get("impersonate_service_account"):
            config["export"]["impersonate"] = export["impersonate_service_account"]
        if export.get("prefix"):
            config["export"]["prefix"] = export["prefix"]
    mailbox = worker.get("mailbox")
    if mailbox is not None:
        if not isinstance(mailbox, dict):
            raise ValueError("Docker target failed: worker.mailbox must be an object")
        config["mailbox"] = {
            "addresses": mailbox.get("addresses"), "trustedAuthservId": mailbox.get("trusted_authserv_id"), "owner": mailbox.get("owner"),
        }
    if key_ref:
        config["coworker"] = {"url": f"http://coworker-api:{COWORKER_API_PORT}/v1"}
    return config, key_ref


def _share_notice_routing(worker_config: dict[str, Any] | None, gateway_config: dict[str, Any] | None) -> None:
    """Make the worker and the gateway agree on who is reached how (``people``) and who is escalated to (``escalate_to``).

    Each keeps its own list, so a list filled in on only one side would leave the other's notices going nowhere. A side that
    omits ``people`` or ``escalate_to`` takes the other's; when both give ``people`` and one person is reached differently, the
    deploy fails instead of guessing which is right.
    """
    if worker_config is None or gateway_config is None:
        return
    for person in sorted(set(worker_config["people"]) & set(gateway_config["people"])):
        w, g = worker_config["people"][person], gateway_config["people"][person]
        if not isinstance(w, dict) or not isinstance(g, dict):
            continue
        if (w.get("channel"), w.get("address", "")) != (g.get("channel"), g.get("address", "")):
            raise ValueError(f"Docker target failed: worker.people and gateway.people disagree about {person}; give them the same channel and address")
    if not worker_config["people"]:
        worker_config["people"] = dict(gateway_config["people"])
    elif not gateway_config["people"]:
        gateway_config["people"] = dict(worker_config["people"])
    if not worker_config.get("escalateTo") and gateway_config.get("escalateTo"):
        worker_config["escalateTo"] = gateway_config["escalateTo"]
    elif not gateway_config.get("escalateTo") and worker_config.get("escalateTo"):
        gateway_config["escalateTo"] = worker_config["escalateTo"]


def _upstream_settings(binding: dict[str, Any]) -> list[str] | None:
    """``EnvironmentBinding.spec.upstream.mcp`` (opt-in): the ``secret://`` references the governed sidecar may resolve for approved upstream MCP.

    Returns ``None`` when upstream MCP is not enabled (the sidecar then refuses every upstream call). Each reference becomes one
    environment variable on the governed sidecar alone (``upstream_http.secret_env_name``); the Hermes process never receives it.
    Enabling this installs a transport, it does not approve or activate any connection: that still needs the owner's consent, a
    separate operator, reviewed fingerprints and the coworker's own grant.
    """
    upstream = binding.get("spec", {}).get("upstream")
    if upstream is None:
        return None
    if not isinstance(upstream, dict) or set(upstream) - {"mcp"}:
        raise ValueError("Docker target failed: EnvironmentBinding upstream holds only mcp")
    mcp = upstream["mcp"]
    if not isinstance(mcp, dict) or set(mcp) - {"enabled", "secret_refs", "vault_key_ref"}:
        raise ValueError("Docker target failed: upstream.mcp holds only enabled, secret_refs and vault_key_ref")
    if mcp.get("enabled") is not True:
        return None
    refs = mcp.get("secret_refs", [] if "vault_key_ref" in mcp else None)
    # Owner-entered credentials live in the sealed vault, so a deployment with a vault key may have no deploy-time references.
    if not isinstance(refs, list) or (not refs and "vault_key_ref" not in mcp) or len(refs) > 50 or not all(
        isinstance(item, str) and item.startswith("secret://") and len(item) > len("secret://") for item in refs
    ):
        raise ValueError("Docker target failed: upstream.mcp.secret_refs must list secret:// references")
    return sorted(set(refs))


def _vault_key_ref(binding: dict[str, Any]) -> str | None:
    """``upstream.mcp.vault_key_ref``: the sealed vault's private key, injected into the governed sidecar alone.

    With it the sidecar can open credentials a client owner entered through an enrollment link (``sealed_vault``); the gateway only
    ever gets the matching public key (``gateway.enrollment.vault_public_key``). Without it, only deploy-time references resolve.
    """
    if _upstream_settings(binding) is None:
        return None
    ref = binding["spec"]["upstream"]["mcp"].get("vault_key_ref")
    if ref is None:
        return None
    if not isinstance(ref, str) or not ref.startswith("secret://") or len(ref) <= len("secret://"):
        raise ValueError("Docker target failed: upstream.mcp.vault_key_ref must be a secret:// reference")
    return ref


def _check_vault_pair(gateway_config: dict[str, Any] | None, private_key: str | None) -> None:
    """Refuse to deploy when the gateway would seal owner-entered credentials to a key the sidecar cannot open."""
    public_key = ((gateway_config or {}).get("enrollment") or {}).get("vaultPublicKey")
    if public_key is None or private_key is None:
        return
    spec = importlib.util.spec_from_file_location("model_garden_deploy_sealed_vault", ROOT / "platform" / "core" / "sealed_vault.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        matches = module.public_key_of(private_key) == public_key
    except module.VaultError:
        matches = False
    if not matches:
        raise ValueError("Docker target failed: gateway.enrollment.vault_public_key does not match the key behind upstream.mcp.vault_key_ref")


def _upstream_env(refs: list[str]) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("model_garden_deploy_upstream_http", ROOT / "platform" / "runtime" / "upstream_http.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return {module.secret_env_name(ref): ref for ref in refs}


GATEWAY_CONFIG_PATH = pathlib.Path(".modelgarden") / "gateway.json"
GATEWAY_HTTP_PORT = 8080
GATEWAY_SMTP_PORT = 2525
GATEWAY_SECRET_ENV = dict([
    ("slack_signing_secret_ref", "MODEL_GARDEN_SLACK_SIGNING_SECRET"),
    ("slack_bot_token_ref", "MODEL_GARDEN_SLACK_BOT_TOKEN"),
    ("teams_app_secret_ref", "MODEL_GARDEN_TEAMS_APP_SECRET"),
    ("webchat_key_ref", "MODEL_GARDEN_WEBCHAT_KEY"),
    ("approval_link_key_ref", "MODEL_GARDEN_APPROVAL_LINK_KEY"),
    ("smtp_password_ref", "MODEL_GARDEN_SMTP_PASSWORD"),
])


OAUTH_PROVIDER_KEYS = {"preset": "preset", "client_id": "clientId", "authorize_url": "authorizeUrl", "token_url": "tokenUrl", "revoke_url": "revokeUrl",
                       "allowed_hosts": "allowedHosts", "scopes_by_tool": "scopesByTool", "auth_params": "authParams",
                       "implicit_scopes": "implicitScopes", "benign_scopes": "benignScopes", "scope_prefixes": "scopePrefixes"}


def _gateway_oauth(oauth: Any, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """``gateway.oauth``: provider OAuth consent (#66). Non-secret provider declarations plus one client-secret reference each.

    Opt-in. It needs the sealed vault (``gateway.enrollment``), web sign-in and ``public_origin`` (the redirect URI is
    ``<public_origin>/connections/oauth/callback``). Each provider is a ``preset`` (``google``, ``microsoft``) or a generic provider with
    exact ``allowed_hosts``; all endpoints are validated against that allow-list here and again by the gateway. Each client secret is a
    ``secret://`` reference injected into the gateway and the governed sidecar (which refreshes tokens), never rendered into the bundle.
    """
    if not isinstance(oauth, dict) or set(oauth) != {"providers"} or not isinstance(oauth["providers"], dict) or not oauth["providers"]:
        raise ValueError("Docker target failed: gateway.oauth holds only providers (a non-empty object)")
    if "enrollment" not in config or not config.get("publicOrigin"):
        raise ValueError("Docker target failed: gateway.oauth needs gateway.enrollment (the sealed vault), web sign-in and public_origin")
    spec = importlib.util.spec_from_file_location("model_garden_deploy_oauth_consent", ROOT / "platform" / "core" / "oauth_consent.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["model_garden_deploy_oauth_consent"] = module
    spec.loader.exec_module(module)
    providers: dict[str, Any] = {}
    env: dict[str, str] = {}
    for name, raw in oauth["providers"].items():
        allowed = set(OAUTH_PROVIDER_KEYS) | {"client_secret_ref"}
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError(f"Docker target failed: gateway.oauth.providers.{name} holds only: {', '.join(sorted(allowed))}")
        ref = raw.get("client_secret_ref")
        if not isinstance(ref, str) or not ref.startswith("secret://") or len(ref) <= len("secret://"):
            raise ValueError(f"Docker target failed: gateway.oauth.providers.{name}.client_secret_ref must be a secret:// reference")
        providers[name] = {OAUTH_PROVIDER_KEYS[key]: value for key, value in raw.items() if key in OAUTH_PROVIDER_KEYS}
        env[module.client_secret_env(name)] = ref
    try:
        module.load_providers(providers)
    except module.OAuthError as exc:
        raise ValueError(f"Docker target failed: gateway.oauth: {exc}") from None
    return {"providers": providers}, env


def _gateway_settings(binding: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, str]]:
    """``EnvironmentBinding.spec.gateway`` as the non-secret gateway configuration.

    Returns ``(config, secret_env, deploy)``: the configuration rendered into the bundle, a map from environment variable to the
    ``secret://`` reference injected at apply time, and the publish settings. The gateway (``platform/runtime/gateway.py``) is the
    HTTP and SMTP front door for web chat, Slack, Teams and the support mailbox. It is opt-in: without ``gateway.enabled: true``
    the bundle is unchanged. Ports are published on loopback unless ``bind`` says otherwise, so a TLS reverse proxy is the only
    public listener by default.
    """
    spec = binding.get("spec", {})
    gateway = spec.get("gateway")
    if gateway is None or (isinstance(gateway, dict) and gateway.get("enabled") is not True):
        return None, {}, {}
    if not isinstance(gateway, dict):
        raise ValueError("Docker target failed: EnvironmentBinding gateway must be an object")
    allowed = {"enabled", "public_origin", "agent", "directory", "people", "escalate_to", "web", "slack", "teams", "approvals", "mailbox",
               "coworker_api_key_ref", "secrets", "smtp_user", "bind", "smtp_bind", "enrollment", "oauth"}
    unknown = sorted(set(gateway) - allowed)
    if unknown:
        raise ValueError(f"Docker target failed: unknown gateway settings: {', '.join(unknown)}")
    secrets = gateway.get("secrets") or {}
    if not isinstance(secrets, dict) or set(secrets) - set(GATEWAY_SECRET_ENV):
        raise ValueError(f"Docker target failed: gateway.secrets holds only: {', '.join(sorted(GATEWAY_SECRET_ENV))}")
    secret_env: dict[str, str] = {}
    for name, ref in secrets.items():
        if not isinstance(ref, str) or not ref.startswith("secret://") or len(ref) <= len("secret://"):
            raise ValueError(f"Docker target failed: gateway.secrets.{name} must be a secret:// reference")
        secret_env[GATEWAY_SECRET_ENV[name]] = ref
    key_ref = gateway.get("coworker_api_key_ref")
    if key_ref is not None:
        if not isinstance(key_ref, str) or not key_ref.startswith("secret://") or len(key_ref) <= len("secret://"):
            raise ValueError("Docker target failed: gateway.coworker_api_key_ref must be a secret:// reference")
        secret_env["MODEL_GARDEN_COWORKER_API_KEY"] = key_ref
    for name in ("bind", "smtp_bind"):
        value = gateway.get(name, "127.0.0.1")
        if not isinstance(value, str) or not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", value):
            raise ValueError(f"Docker target failed: gateway.{name} must be an IPv4 address")
    config: dict[str, Any] = {
        "publicOrigin": gateway.get("public_origin"), "directory": gateway.get("directory") or {}, "people": gateway.get("people") or {},
    }
    for source, target in (("escalate_to", "escalateTo"), ("agent", "agent")):
        if gateway.get(source):
            config[target] = gateway[source]
    web = gateway.get("web")
    if web is not None:
        mail = (web or {}).get("mail") or {}
        config["web"] = {"allowedAddresses": web.get("allowed_addresses"), "mail": {"from": mail.get("from"), "host": mail.get("host"), **({"port": mail["port"]} if "port" in mail else {})}}
        required = {"MODEL_GARDEN_WEBCHAT_KEY"}
    else:
        required = set()
    slack = gateway.get("slack")
    if slack is not None:
        config["slack"] = {"botUserId": slack.get("bot_user_id"), "allowedChannels": slack.get("allowed_channels") or [],
                           **({"uploadFiles": True} if slack.get("upload_files") is True else {})}
        required |= {"MODEL_GARDEN_SLACK_SIGNING_SECRET", "MODEL_GARDEN_SLACK_BOT_TOKEN"}
    teams = gateway.get("teams")
    if teams is not None:
        config["teams"] = {"appId": teams.get("app_id"), "tenants": teams.get("tenants"), "allowedConversations": teams.get("allowed_conversations") or []}
        required |= {"MODEL_GARDEN_TEAMS_APP_SECRET"}
    approvals = gateway.get("approvals")
    if approvals is not None:
        config["approvals"] = {"emailLinks": bool(approvals.get("email_links")), "notify": approvals.get("notify") or []}
        if approvals.get("receipts") is not None:
            if not isinstance(approvals["receipts"], bool):
                raise ValueError("Docker target failed: gateway.approvals.receipts must be true or false")
            config["approvals"]["receipts"] = approvals["receipts"]
        if approvals.get("email_links"):
            required |= {"MODEL_GARDEN_APPROVAL_LINK_KEY"}
    mailbox = gateway.get("mailbox")
    if mailbox is not None:
        config["mailbox"] = {"addresses": mailbox.get("addresses"), "hostname": mailbox.get("hostname", "mail"), "trustedRelays": mailbox.get("trusted_relays") or []}
    if key_ref:
        config["coworker"] = {"url": f"http://coworker-api:{COWORKER_API_PORT}/v1"}
    enrollment = gateway.get("enrollment")
    if enrollment is not None:
        if not isinstance(enrollment, dict) or set(enrollment) != {"vault_public_key"} or web is None:
            raise ValueError("Docker target failed: gateway.enrollment holds vault_public_key and needs web sign-in")
        if _vault_key_ref(binding) is None:
            # Without the sidecar's private key an owner's entry would be saved and never usable.
            raise ValueError("Docker target failed: gateway.enrollment needs upstream.mcp enabled with vault_key_ref")
        config["enrollment"] = {"vaultPublicKey": enrollment["vault_public_key"]}
    if gateway.get("oauth") is not None:
        config["oauth"], oauth_env = _gateway_oauth(gateway["oauth"], config)
        secret_env.update(oauth_env)
    missing = sorted(required - set(secret_env))
    if missing:
        raise ValueError(f"Docker target failed: gateway needs secrets for its channels: {', '.join(missing)}")
    if not any(key in config for key in ("web", "slack", "teams", "mailbox")):
        raise ValueError("Docker target failed: gateway needs at least one of web, slack, teams or mailbox")
    deploy = {"bind": gateway.get("bind", "127.0.0.1"), "smtp_bind": gateway.get("smtp_bind", "127.0.0.1")}
    if gateway.get("smtp_user"):
        deploy["smtp_user"] = str(gateway["smtp_user"])
    return config, secret_env, deploy


def _entrypoint() -> str:
    return """#!/bin/sh
set -eu
mkdir -p "$HERMES_HOME/.modelgarden" "$HERMES_HOME/skills"
cp /opt/profile-seed/SOUL.md "$HERMES_HOME/SOUL.md"
cp /opt/profile-seed/config.yaml "$HERMES_HOME/config.yaml"
cp /opt/profile-seed/.modelgarden/desired-state.json "$HERMES_HOME/.modelgarden/desired-state.json"
rm -rf "$HERMES_HOME/skills/model-garden"
if [ -d /opt/profile-seed/skills/model-garden ]; then
  mkdir -p "$HERMES_HOME/skills"
  cp -R /opt/profile-seed/skills/model-garden "$HERMES_HOME/skills/model-garden"
fi
exec "$@"
"""


def _dockerfile(lock: dict[str, str]) -> str:
    return f"""FROM {BASE_IMAGE}
ARG HERMES_VERSION={lock['version']}
ARG HERMES_COMMIT={lock['commit']}
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    HERMES_HOME=/opt/data
COPY hermes-source /opt/hermes-agent
COPY model-garden-platform /opt/model-garden-platform
RUN test \"$(git -C /opt/hermes-agent rev-parse HEAD 2>/dev/null || true)\" = \"$HERMES_COMMIT\" || \\
      test \"$(cat /opt/hermes-agent/.model-garden-hermes-commit)\" = \"$HERMES_COMMIT\" && \\
    python -m pip install --no-cache-dir -e /opt/hermes-agent -r /opt/model-garden-platform/requirements-runtime.txt && \\
    test \"$(python -c 'import importlib.metadata; print(importlib.metadata.version(\"hermes-agent\"))')\" = \"$HERMES_VERSION\"
COPY profile /opt/profile-seed
COPY entrypoint.sh /usr/local/bin/model-garden-entrypoint
RUN chmod 0555 /usr/local/bin/model-garden-entrypoint
ENTRYPOINT [\"/usr/local/bin/model-garden-entrypoint\"]
CMD [\"hermes\", \"serve\", \"--host\", \"127.0.0.1\", \"--port\", \"9119\", \"--skip-build\"]
"""


def _healthcheck(port: int) -> dict[str, Any]:
    return {
        "test": [
            "CMD",
            "python",
            "-c",
            f"import socket; s=socket.create_connection(('127.0.0.1',{port}),5); s.close()",
        ],
        "interval": "10s",
        "timeout": "5s",
        "retries": 6,
        "start_period": "20s",
    }


def _compose(
    project_name: str,
    provider: str,
    lock: dict[str, str],
    *,
    read_audit: str | None = None,
    approval_ttl_hours: str | None = None,
    built_in_tools: bool = False,
    worker: bool = False,
    coworker_api: bool = False,
    gateway: tuple[dict[str, Any], dict[str, str], dict[str, str]] | None = None,
    upstream_refs: list[str] | None = None,
    vault_key: bool = False,
) -> str:
    provider_env = PROVIDER_ENV[provider]
    hermes: dict[str, Any] = {
        "build": {
            "context": ".",
            "args": {
                "HERMES_VERSION": lock["version"],
                "HERMES_COMMIT": lock["commit"],
            },
        },
        "image": f"model-garden-{project_name}:current",
        "restart": "unless-stopped",
        "environment": {
            "HERMES_HOME": "/opt/data",
            provider_env: "${MODEL_GARDEN_MODEL_CREDENTIAL:?MODEL_GARDEN_MODEL_CREDENTIAL is required}",
        },
        "volumes": ["hermes-data:/opt/data"],
        "healthcheck": _healthcheck(9119),
    }
    services: dict[str, Any] = {"hermes": hermes}
    volumes: dict[str, Any] = {"hermes-data": {}}
    if built_in_tools:
        services["governed-tools"] = {
            "image": f"model-garden-{project_name}:current",
            "restart": "unless-stopped",
            "entrypoint": ["python", "/opt/model-garden-platform/platform/runtime/governed_mcp.py"],
            "command": ["--host", "0.0.0.0", "--port", "9120"],
            "environment": {
                "MODEL_GARDEN_DESIRED_STATE": "/opt/profile-seed/.modelgarden/desired-state.json",
                "MODEL_GARDEN_AUDIT_PATH": "/opt/data/.modelgarden/audit.jsonl",
                "MODEL_GARDEN_APPROVAL_ROOT": "/opt/data/.modelgarden/approvals",
                "MODEL_GARDEN_INITIATING_IDENTITY": "runtime:hermes",
                "MODEL_GARDEN_DEPLOYED_BY": "${MODEL_GARDEN_DEPLOYED_BY:-}",
                "MODEL_GARDEN_CAPABILITY_MANIFEST": f"/opt/profile-seed/{MANIFEST_PATH.as_posix()}",
                **({"MODEL_GARDEN_READ_AUDIT": read_audit} if read_audit else {}),
                **({"MODEL_GARDEN_APPROVAL_TTL_HOURS": approval_ttl_hours} if approval_ttl_hours else {}),
                **({"MODEL_GARDEN_UPSTREAM_MCP": "enabled",
                    **{name: f"${{{name}:?{name} is required}}" for name in sorted(_upstream_env(upstream_refs))}}
                   if upstream_refs is not None else {}),
                **({"MODEL_GARDEN_VAULT_PRIVATE_KEY": "${MODEL_GARDEN_VAULT_PRIVATE_KEY:?MODEL_GARDEN_VAULT_PRIVATE_KEY is required}"}
                   if vault_key else {}),
            },
            "volumes": ["governance-data:/opt/data"],
            "healthcheck": _healthcheck(9120),
        }
        volumes["governance-data"] = {}
        hermes["depends_on"] = {"governed-tools": {"condition": "service_healthy"}}
    if coworker_api:
        # A second Hermes process serving only the OpenAI-compatible API server on the private Compose network, so the
        # worker can ask the coworker to carry out a scheduled task or answer an email. It has its own state volume.
        services["coworker-api"] = {
            "image": f"model-garden-{project_name}:current",
            "restart": "unless-stopped",
            "command": ["hermes", "gateway", "run"],
            "environment": {
                "HERMES_HOME": "/opt/data",
                provider_env: "${MODEL_GARDEN_MODEL_CREDENTIAL:?MODEL_GARDEN_MODEL_CREDENTIAL is required}",
                "API_SERVER_ENABLED": "true",
                "API_SERVER_HOST": "0.0.0.0",
                "API_SERVER_PORT": str(COWORKER_API_PORT),
                "API_SERVER_KEY": "${MODEL_GARDEN_COWORKER_API_KEY:?MODEL_GARDEN_COWORKER_API_KEY is required}",
            },
            "volumes": ["coworker-data:/opt/data"],
            "healthcheck": _healthcheck(COWORKER_API_PORT),
            **({"depends_on": {"governed-tools": {"condition": "service_healthy"}}} if built_in_tools else {}),
        }
        volumes["coworker-data"] = {}
    if worker:
        environment = {
            "MODEL_GARDEN_DESIRED_STATE": "/opt/profile-seed/.modelgarden/desired-state.json",
            "MODEL_GARDEN_DATA_ROOT": "/opt/data/.modelgarden",
            "MODEL_GARDEN_WORKER_CONFIG": f"/opt/profile-seed/{WORKER_CONFIG_PATH.as_posix()}",
            "MODEL_GARDEN_CAPABILITY_MANIFEST": f"/opt/profile-seed/{MANIFEST_PATH.as_posix()}",
            "MODEL_GARDEN_DEPLOYED_BY": "${MODEL_GARDEN_DEPLOYED_BY:-}",
            **({"MODEL_GARDEN_APPROVAL_TTL_HOURS": approval_ttl_hours} if approval_ttl_hours else {}),
        }
        if coworker_api:
            environment["MODEL_GARDEN_COWORKER_API_KEY"] = "${MODEL_GARDEN_COWORKER_API_KEY:?MODEL_GARDEN_COWORKER_API_KEY is required}"
        # Each Hermes state volume is mounted read-only so the worker can meter model usage from its session store.
        usage_stores = {"conversations": "hermes-data", **({"coworker": "coworker-data"} if coworker_api else {})}
        environment["MODEL_GARDEN_USAGE_SOURCES"] = ",".join(f"{name}=/opt/hermes/{name}/state.db" for name in usage_stores)
        depends = {}
        if built_in_tools:
            depends["governed-tools"] = {"condition": "service_healthy"}
        if coworker_api:
            depends["coworker-api"] = {"condition": "service_healthy"}
        services["governed-worker"] = {
            "image": f"model-garden-{project_name}:current",
            "restart": "unless-stopped",
            "entrypoint": ["python", "/opt/model-garden-platform/platform/runtime/worker.py"],
            "environment": environment,
            "volumes": ["governance-data:/opt/data", *(f"{volume}:/opt/hermes/{name}:ro" for name, volume in usage_stores.items())],
            "healthcheck": {
                "test": ["CMD", "python", "/opt/model-garden-platform/platform/runtime/worker.py", "--health"],
                "interval": "30s", "timeout": "10s", "retries": 6, "start_period": "60s",
            },
            **({"depends_on": depends} if depends else {}),
        }
        volumes["governance-data"] = {}
    if gateway is not None:
        config, secret_env, deploy = gateway
        environment = {
            "MODEL_GARDEN_DESIRED_STATE": "/opt/profile-seed/.modelgarden/desired-state.json",
            "MODEL_GARDEN_DATA_ROOT": "/opt/data/.modelgarden",
            "MODEL_GARDEN_GATEWAY_CONFIG": f"/opt/profile-seed/{GATEWAY_CONFIG_PATH.as_posix()}",
            "MODEL_GARDEN_CAPABILITY_MANIFEST": f"/opt/profile-seed/{MANIFEST_PATH.as_posix()}",
            **{name: f"${{{name}:?{name} is required}}" for name in sorted(secret_env)},
            **({"MODEL_GARDEN_SMTP_USER": deploy["smtp_user"]} if "smtp_user" in deploy else {}),
            **({"MODEL_GARDEN_APPROVAL_TTL_HOURS": approval_ttl_hours} if approval_ttl_hours else {}),
        }
        ports = []
        if any(key in config for key in ("web", "slack", "teams")) or config.get("approvals", {}).get("emailLinks"):
            ports.append(f"{deploy['bind']}:{GATEWAY_HTTP_PORT}:{GATEWAY_HTTP_PORT}")
        if "mailbox" in config:
            ports.append(f"{deploy['smtp_bind']}:{GATEWAY_SMTP_PORT}:{GATEWAY_SMTP_PORT}")
        depends = {}
        if built_in_tools:
            depends["governed-tools"] = {"condition": "service_healthy"}
        if coworker_api:
            depends["coworker-api"] = {"condition": "service_healthy"}
        services["channel-gateway"] = {
            "image": f"model-garden-{project_name}:current",
            "restart": "unless-stopped",
            "entrypoint": ["python", "/opt/model-garden-platform/platform/runtime/gateway.py"],
            "environment": environment,
            "volumes": ["governance-data:/opt/data"],
            "ports": ports,
            "healthcheck": {
                "test": ["CMD", "python", "/opt/model-garden-platform/platform/runtime/gateway.py", "--health"],
                "interval": "30s", "timeout": "10s", "retries": 6, "start_period": "30s",
            },
            **({"depends_on": depends} if depends else {}),
        }
        volumes["governance-data"] = {}
        if "oauth" in config and "governed-tools" in services:  # the sidecar refreshes tokens on use, so it holds the client secrets too
            sidecar = services["governed-tools"]["environment"]
            sidecar["MODEL_GARDEN_OAUTH_CONFIG"] = f"/opt/profile-seed/{GATEWAY_CONFIG_PATH.as_posix()}"
            sidecar.update({name: f"${{{name}:?{name} is required}}" for name in sorted(secret_env) if name.endswith("_CLIENT_SECRET")})
        if worker and "MODEL_GARDEN_SLACK_BOT_TOKEN" in secret_env:  # the worker delivers notices to Slack direct messages
            services["governed-worker"]["environment"]["MODEL_GARDEN_SLACK_BOT_TOKEN"] = "${MODEL_GARDEN_SLACK_BOT_TOKEN:?MODEL_GARDEN_SLACK_BOT_TOKEN is required}"
    document = {"services": services, "volumes": volumes}
    return yaml.safe_dump(document, sort_keys=False)


def _clone_hermes(lock: dict[str, str], destination: pathlib.Path) -> None:
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--filter=blob:none", lock["repository"], str(destination)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(destination), "checkout", "--quiet", lock["commit"]],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        actual = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        raise ValueError(f"Docker target failed: cannot fetch pinned Hermes revision: {detail}") from exc
    if actual != lock["commit"]:
        raise ValueError(f"Docker target failed: Hermes checkout mismatch: expected {lock['commit']}, got {actual}")
    shutil.rmtree(destination / ".git", ignore_errors=True)
    (destination / ".model-garden-hermes-commit").write_text(lock["commit"] + "\n", encoding="utf-8")


def _copy_runtime_source(platform_root: pathlib.Path, destination: pathlib.Path) -> None:
    for relative in RUNTIME_SOURCE_PATHS:
        source = platform_root / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Docker target failed: missing runtime source file {relative.as_posix()}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def render_bundle(
    profile_dir: pathlib.Path,
    binding_path: pathlib.Path,
    bundle_dir: pathlib.Path,
    project_name: str,
    *,
    platform_root: pathlib.Path = ROOT,
    fetch_source: bool = False,
) -> dict[str, Any]:
    if not PROJECT_NAME.fullmatch(project_name):
        raise ValueError("Docker target failed: project name must be lowercase letters, numbers, and internal hyphens")
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise ValueError(f"Docker target failed: refusing to overwrite non-empty bundle directory {bundle_dir}")
    provider = _validate_profile(profile_dir)
    selected_tools = _selected_tools(profile_dir)
    binding = _load_json(binding_path, "EnvironmentBinding")
    environment, credential_ref = _validate_binding(binding)
    if selected_tools & CALENDAR_TOOLS:
        raise ValueError("Docker target failed: native Calendar connectors were retired; an approved upstream MCP route is required before deploying Calendar Tools")
    manifest = _capability_manifest(_desired_state(profile_dir), binding)
    worker_config, coworker_key_ref = _worker_settings(binding)
    gateway = _gateway_settings(binding)
    _share_notice_routing(worker_config, gateway[0])
    if gateway[0] is not None and gateway[1].get("MODEL_GARDEN_COWORKER_API_KEY"):
        if coworker_key_ref not in (None, gateway[1]["MODEL_GARDEN_COWORKER_API_KEY"]):
            raise ValueError("Docker target failed: worker and gateway must share one coworker API key reference")
        coworker_key_ref = gateway[1]["MODEL_GARDEN_COWORKER_API_KEY"]
    read_audit = _read_audit_mode(binding)
    approval_ttl_hours = _approval_ttl_hours(binding)
    upstream_refs = _upstream_settings(binding)
    built_in_tools = bool(selected_tools & BUILT_IN_TOOLS) or upstream_refs is not None
    lock = _load_hermes_lock(platform_root)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(profile_dir, bundle_dir / "profile")
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (bundle_dir / "profile" / MANIFEST_PATH).write_text(manifest_text, encoding="utf-8")
    if worker_config is not None:
        (bundle_dir / "profile" / WORKER_CONFIG_PATH).write_text(json.dumps(worker_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if gateway[0] is not None:
        (bundle_dir / "profile" / GATEWAY_CONFIG_PATH).write_text(json.dumps(gateway[0], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _copy_runtime_source(platform_root, bundle_dir / "model-garden-platform")
    normalized_binding = json.dumps(binding, indent=2, sort_keys=True) + "\n"
    (bundle_dir / "environment-binding.json").write_text(normalized_binding, encoding="utf-8")
    (bundle_dir / "entrypoint.sh").write_text(_entrypoint(), encoding="utf-8")
    (bundle_dir / "Dockerfile").write_text(_dockerfile(lock), encoding="utf-8")
    (bundle_dir / "compose.yaml").write_text(
        _compose(project_name, provider, lock, read_audit=read_audit, approval_ttl_hours=approval_ttl_hours, built_in_tools=built_in_tools,
                 worker=worker_config is not None, coworker_api=coworker_key_ref is not None,
                 gateway=gateway if gateway[0] is not None else None, upstream_refs=upstream_refs,
                 vault_key=_vault_key_ref(binding) is not None),
        encoding="utf-8",
    )
    metadata = {
        "apiVersion": "modelgarden.ai/v1",
        "kind": "DockerDeploymentBundle",
        "project": project_name,
        "environment": environment,
        "provider": provider,
        "modelCredentialRef": credential_ref,
        "selectedTools": sorted(selected_tools),
        "governedToolService": built_in_tools,
        "runtimeWorker": worker_config is not None,
        "coworkerApi": coworker_key_ref is not None,
        "channelGateway": gateway[0] is not None,
        "upstreamMcp": upstream_refs is not None,
        "hermes": {"version": lock["version"], "revision": lock["commit"]},
        "bindingSha256": hashlib.sha256(normalized_binding.encode("utf-8")).hexdigest(),
        "capabilityManifestSha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
        "unboundCapabilities": sorted(
            entry["family"] for entry in manifest["capabilities"] if entry["status"] == "unbound" and (entry["required"] or entry["optional"])
        ),
    }
    (bundle_dir / "deployment-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if fetch_source:
        _clone_hermes(lock, bundle_dir / "hermes-source")
    return metadata


def _run(command: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def _image_exists(image: str, env: dict[str, str]) -> bool:
    result = _run(["docker", "image", "inspect", image], env=env, check=False)
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "").strip()
    if "No such image" in detail or "No such object" in detail:
        return False
    raise ValueError(
        "Docker target failed: cannot inspect current runtime image before deployment: "
        + (detail or "docker image inspect failed")
    )


def _wait_healthy(project_name: str, bundle_dir: pathlib.Path, env: dict[str, str], timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    compose_file = bundle_dir / "compose.yaml"
    document = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = sorted(document.get("services", {})) if isinstance(document, dict) else []
    if not services:
        return False
    compose = ["docker", "compose", "-p", project_name, "-f", str(compose_file)]
    while time.monotonic() < deadline:
        all_healthy = True
        for service in services:
            container = _run([*compose, "ps", "-q", service], env=env, check=False).stdout.strip()
            if not container:
                all_healthy = False
                continue
            status = _run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                    container,
                ],
                env=env,
                check=False,
            ).stdout.strip()
            if status in {"exited", "dead", "unhealthy"}:
                return False
            if status != "healthy":
                all_healthy = False
        if all_healthy:
            return True
        time.sleep(2)
    return False


def apply_bundle(
    bundle_dir: pathlib.Path,
    binding: dict[str, Any],
    project_name: str,
    *,
    secrets_json: str | None,
    timeout: int = 90,
) -> None:
    _, model_ref = _validate_binding(binding)
    resolved = resolve_secrets(binding, secrets_json)
    model_secret = resolved[model_ref]

    runtime_env = os.environ.copy()
    runtime_env.pop("MODEL_GARDEN_RUNTIME_SECRETS_JSON", None)
    runtime_env["MODEL_GARDEN_MODEL_CREDENTIAL"] = model_secret
    _, coworker_key_ref = _worker_settings(binding)
    if coworker_key_ref is not None:
        runtime_env["MODEL_GARDEN_COWORKER_API_KEY"] = resolved[coworker_key_ref]
    for name, ref in _upstream_env(_upstream_settings(binding) or []).items():
        runtime_env[name] = resolved[ref]
    vault_ref = _vault_key_ref(binding)
    if vault_ref is not None:
        runtime_env["MODEL_GARDEN_VAULT_PRIVATE_KEY"] = resolved[vault_ref]
    gateway_config, gateway_secrets, _ = _gateway_settings(binding)
    _check_vault_pair(gateway_config, resolved[vault_ref] if vault_ref is not None else None)
    for name, ref in gateway_secrets.items():
        runtime_env[name] = resolved[ref]

    compose = ["docker", "compose", "-p", project_name, "-f", str(bundle_dir / "compose.yaml")]
    image = f"model-garden-{project_name}:current"
    rollback = f"model-garden-{project_name}:rollback"

    _run(["docker", "compose", "version"], env=runtime_env)
    had_previous = _image_exists(image, runtime_env)
    if had_previous:
        _run(["docker", "tag", image, rollback], env=runtime_env)

    try:
        _run([*compose, "build"], env=runtime_env)
        _run([*compose, "up", "-d", "--remove-orphans"], env=runtime_env)
        if not _wait_healthy(project_name, bundle_dir, runtime_env, timeout):
            raise RuntimeError("candidate client runtime did not become healthy")
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        if had_previous:
            _run(["docker", "tag", rollback, image], env=runtime_env, check=False)
            _run([*compose, "up", "-d", "--no-build", "--force-recreate"], env=runtime_env, check=False)
            restored = _wait_healthy(project_name, bundle_dir, runtime_env, timeout)
            suffix = "; previous runtime restored" if restored else "; rollback also failed health verification"
        else:
            _run([*compose, "down"], env=runtime_env, check=False)
            suffix = "; no previous runtime existed"
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        raise ValueError(f"Docker target failed: deployment verification failed: {detail}{suffix}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_dir", type=pathlib.Path, help="generated Hermes profile directory")
    parser.add_argument("environment_binding", type=pathlib.Path, help="validated EnvironmentBinding JSON")
    parser.add_argument("bundle_dir", type=pathlib.Path, help="empty directory for the Docker deployment bundle")
    parser.add_argument("--project-name", required=True, help="stable lowercase client-environment deployment name")
    parser.add_argument("--apply", action="store_true", help="build/update the target runtime using Docker Compose")
    parser.add_argument("--health-timeout", type=int, default=90, help="seconds to wait for all runtime services to become healthy")
    args = parser.parse_args()

    try:
        metadata = render_bundle(
            args.profile_dir,
            args.environment_binding,
            args.bundle_dir,
            args.project_name,
            fetch_source=args.apply,
        )
        if args.apply:
            binding = _load_json(args.environment_binding, "EnvironmentBinding")
            apply_bundle(
                args.bundle_dir,
                binding,
                args.project_name,
                secrets_json=os.environ.get("MODEL_GARDEN_RUNTIME_SECRETS_JSON"),
                timeout=args.health_timeout,
            )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    action = "Deployed" if args.apply else "Rendered"
    print(
        f"{action} Docker target {metadata['project']} ({metadata['environment']}) "
        f"with Hermes {metadata['hermes']['version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())