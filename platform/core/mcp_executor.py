"""Approved upstream MCP operation execution, without a vendor-specific connector (#87, #158).

This is an internal sidecar seam, NOT an HTTP client or a public model Tool. A vetted
external MCP transport is injected by the deployment. It must dial the pinned public IP
over HTTPS with TLS hostname verification and no redirect following. Until such a
transport, owner consent and retained tenant proof are available, providers stay planned.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

MAX_ARGUMENT_BYTES = 65536
MAX_RESPONSE_BYTES = 1048576


def _sibling(module: str, filename: str):
    if module not in sys.modules:
        spec = importlib.util.spec_from_file_location(module, pathlib.Path(__file__).with_name(filename))
        loaded = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module] = loaded
        spec.loader.exec_module(loaded)
    return sys.modules[module]


def _mcp():
    return _sibling("modelgarden_gateway_passthrough", "mcp_passthrough.py")


def _endpoint():
    return _sibling("modelgarden_gateway_endpoint", "endpoint_guard.py")


def _capabilities():
    return _sibling("modelgarden_gateway_capabilities", "capabilities.py")


class GatewayError(PermissionError):
    """A reviewed route, per-coworker authorization or requested operation is unavailable."""


def _bounded_json(value: Any, limit: int, label: str) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as exc:
        raise GatewayError(f"{label} must be JSON") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise GatewayError(f"{label} exceeds limit")
    return encoded


def prepare(
    declaration: Mapping[str, Any],
    *,
    registry: Any,
    tenant: str,
    attested_agent: str,
    upstream: str,
    enabled_tools: Iterable[str],
    originator_permissions: Iterable[str],
    now: int,
) -> dict[str, Any]:
    """Resolve only the authenticated coworker's grant; no network or secrets are read."""
    mcp = _mcp()
    server = mcp.validate_server(declaration)
    entry = server["tools"].get(upstream)
    if entry is None:
        raise GatewayError("upstream operation is not explicitly allowed")
    tool = entry["tool"]
    catalog = _capabilities().catalog_tool(tool)
    if catalog is None or entry.get("capability") != tool or catalog["risk"] != entry["risk"]:
        raise GatewayError("only an approved portable Tool with the catalog risk can execute")
    if registry.tenant != tenant:
        raise GatewayError("the grant registry belongs to another tenant")
    grant = registry.resolve(
        attested_agent=attested_agent, family=tool.split(".", 1)[0], tool=tool,
        originator_permissions=originator_permissions, enabled_tools=enabled_tools, now=now,
    )
    if grant["provider"] != server["name"] or grant["secretRef"] != server["credentialRef"]:
        raise GatewayError("the coworker grant does not belong to this server and credential")
    if grant["isolation"] not in ("own-identity", "isolated-grant"):
        raise GatewayError("shared provider identity is not authorized for unattended execution")
    return {"server": server, "declaration": copy.deepcopy(dict(declaration)), "upstream": upstream, "tool": tool, "risk": entry["risk"], "grant": grant,
            "ledger": {"provider": "mcp:" + server["name"], "tier": 2,
                       "connectionId": grant["connectionId"], "grantId": grant["grantId"],
                       "actingIdentity": grant["actingIdentity"], "upstreamTool": upstream}}


def invoke(
    route: Mapping[str, Any],
    arguments: Mapping[str, Any],
    *,
    approvals: Any,
    transport_factory: Callable[[str, str, str], Any],
    secret_resolver: Callable[[str], str],
    dns_resolver: Callable[[str], Iterable[str]],
    recheck: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run one operation over a single pinned-address, vetted upstream MCP session.

    transport_factory(url, pinned_ip, credential) returns a context manager yielding
    an MCP session with list_tools() and call_tool(name, arguments). This contract is
    deliberately NOT a raw fetch(url) primitive; transport must enforce pinned-IP
    dialing, normal HTTPS certificate/hostname validation and disabled redirects.
    """
    if not isinstance(arguments, Mapping):
        raise GatewayError("arguments must be an object")
    _bounded_json(arguments, MAX_ARGUMENT_BYTES, "arguments")
    fresh = recheck()
    if (fresh["grant"] != route["grant"] or fresh["declaration"] != route["declaration"]
            or fresh["server"] != route["server"] or fresh.get("connectionRevision") != route.get("connectionRevision")
            or fresh.get("reviewedFingerprints") != route.get("reviewedFingerprints")):
        raise GatewayError("the approved connection changed; retry from a fresh request")
    server, upstream = fresh["server"], fresh["upstream"]
    qualified = _endpoint().qualify(server["url"], dns_resolver)
    destination = qualified["pinned"][0]
    _endpoint().assert_pinned(destination, qualified["pinned"])
    try:
        credential = secret_resolver(route["grant"]["secretRef"])
    except Exception:
        raise GatewayError("upstream credential is unavailable") from None
    if not isinstance(credential, str) or not credential:
        raise GatewayError("upstream credential is unavailable")
    try:
        with transport_factory(server["url"], destination, credential) as session:
            offered = session.list_tools()
            if not isinstance(offered, list) or len(offered) > _mcp().MAX_TOOLS * 5:
                raise GatewayError("unbounded upstream Tool listing")
            selected = next((t for t in offered if isinstance(t, Mapping) and t.get("name") == upstream), None)
            if selected is None:
                raise GatewayError("approved upstream operation is absent")
            if sum(1 for t in offered if isinstance(t, Mapping) and t.get("name") == upstream) != 1:
                raise GatewayError("duplicate upstream operation name")
            gate = _mcp().call_gate(fresh["declaration"], selected, approvals)
            reviewed = route.get("reviewedFingerprints")
            if reviewed is not None and reviewed.get(upstream) != _mcp().fingerprint(selected):
                raise GatewayError("operation differs from the last activated review")
            if gate["tool"] != route["tool"] or gate["risk"] != route["risk"]:
                raise GatewayError("upstream operation mapping changed")
            schema = selected.get("inputSchema")
            if not isinstance(schema, Mapping):
                raise GatewayError("upstream input schema is absent")
            try:
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(dict(arguments))
            except Exception:
                # Invalid schemas and validation errors may embed confidential arguments.
                raise GatewayError("upstream operation input is invalid") from None
            latest = recheck()
            if (latest["grant"] != route["grant"] or latest.get("connectionRevision") != route.get("connectionRevision")
                    or latest.get("reviewedFingerprints") != route.get("reviewedFingerprints")):
                raise GatewayError("connection revoked or replaced before execution")
            result = session.call_tool(upstream, dict(arguments))
    except (GatewayError, _mcp().SchemaDriftError, ValidationError):
        raise
    except Exception:
        # Provider/transport errors can contain bearer credentials or customer data.
        raise RuntimeError("upstream MCP transport unavailable") from None
    if not isinstance(result, dict):
        raise GatewayError("upstream result must be an object")
    encoded = _bounded_json(result, MAX_RESPONSE_BYTES, "upstream result")
    if len(credential) >= 12 and credential in encoded:
        raise GatewayError("upstream response leaked an authorization credential")
    return result
