"""One governed seam from a portable Tool call to an approved upstream operation, whichever backend serves it (#158, #87).

The governed sidecar must be able to serve ``crm.read`` (say) from an official MCP server today and from a managed integration
platform tomorrow, with the Skill unchanged and the same governance either way. This is the credential-free routing core. It is
**not wired into the sidecar**, holds no credential and makes no network call: a backend is an injected object. Two adapters
(``McpBackend`` for an MCP client, ``ManagedBackend`` for a managed or self-hosted integration platform's "run this action") turn
their client's native shapes into one normalised result.

For every call the router enforces, in order, and fails closed:

1. **Tenant.** The router belongs to one tenant; a call for another tenant is refused.
2. **Declared route only.** The tool must be one a declared, validated server (listed under the backend that serves it) exposes (``mcp_passthrough.validate_server``); there is
   no route for anything else, so there is no way to make an arbitrary HTTP or MCP call through it.
3. **Approved, pinned, safe definition.** The live upstream definition must be the approved fingerprint and pass the metadata review
   (``mcp_passthrough.call_gate``); drift, an unapproved tool or a poisoned description refuses the call.
4. **The coworker's own grant.** The attested caller must hold a usable grant of its own covering the tool
   (``provider_grants.GrantRegistry.resolve``). A shared token is never substituted, and another coworker's grant cannot be used.
5. **Exact-action approval for anything but a read.** The caller passes the approval it holds for this exact ``requestId``; a missing
   or different one refuses a write, privileged or external-communication call.
6. **Replay.** A ``requestId`` that returns with different arguments is refused; the same call repeats safely because the
   idempotency key sent upstream is derived from tenant, coworker, tool and request id.
7. **Execution and normalisation.** The backend gets the operation, the arguments, the grant's secret *reference* (never a value)
   and the idempotency key. Whatever it returns is bounded and marked ``untrusted``; any failure becomes one of
   ``rate-limited``, ``outage``, ``unauthorized`` or ``rejected`` with no upstream text, and ``unauthorized`` marks the grant failed so
   the owner is told through the existing connection notices.
8. **Ledger fields.** The result carries the attribution a ledger entry needs (``receipt_fields``: coworker, grant, acting identity,
   provider) and an upstream receipt id when the backend gave a plain one, never arguments or results.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

ERROR_CODES = ("rate-limited", "outage", "unauthorized", "rejected")
MAX_RESULT_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 32 * 1024
SEEN_LIMIT = 2000
_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/=-]{0,127}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
Audit = Callable[[str, str, dict[str, Any]], Any]


class UpstreamError(RuntimeError):
    """A backend failure with a ``code`` from ``ERROR_CODES``; its message is never shown or ledgered."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in ERROR_CODES else "outage"


class RouteRefused(PermissionError):
    """The router refused before anything was sent upstream; ``reason`` is a short stable code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class Backend(Protocol):
    def execute(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return ``{"result": ..., "receipt": optional id}`` or raise ``UpstreamError``."""


def _module(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _mcp():
    return _module("model_garden_router_mcp", "mcp_passthrough.py")


def _grants():
    return _module("model_garden_router_grants", "provider_grants.py")


# -- backend adapters ---------------------------------------------------------------------------


class McpBackend:
    """Adapts an MCP client exposing ``call_tool(name, arguments, credential_ref=..., idempotency_key=...) -> {content, isError}``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def execute(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._client.call_tool(request["operation"], dict(request["arguments"]), credential_ref=request["secretRef"],
                                          idempotency_key=request["idempotencyKey"])
        if not isinstance(response, Mapping):
            raise UpstreamError("outage")
        if response.get("isError"):
            raise UpstreamError(str(response.get("errorCode") or "rejected"))
        parts = [item.get("text") for item in response.get("content", []) if isinstance(item, Mapping) and item.get("type") == "text"]
        result: Any = response.get("structuredContent") if response.get("structuredContent") is not None else "\n".join(part for part in parts if isinstance(part, str))
        return {"result": result, "receipt": response.get("receiptId")}


class ManagedBackend:
    """Adapts a managed or self-hosted integration platform exposing ``run(action, input, connection_ref, idempotency_key) -> {output, executionId}``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def execute(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._client.run(request["operation"], dict(request["arguments"]), connection_ref=request["secretRef"], idempotency_key=request["idempotencyKey"])
        if not isinstance(response, Mapping):
            raise UpstreamError("outage")
        if response.get("error"):
            raise UpstreamError(str(response.get("error")))
        return {"result": response.get("output"), "receipt": response.get("executionId")}


# -- the router ---------------------------------------------------------------------------------


class UpstreamRouter:
    def __init__(
        self,
        *,
        tenant: str,
        servers: Mapping[str, Iterable[Mapping[str, Any]]],
        approvals: Any,
        grants: Any,
        backends: Mapping[str, Backend],
        audit: Audit | None = None,
    ) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise ValueError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self._approvals, self._grants, self._audit = approvals, grants, audit
        self._backends = dict(backends)
        self._routes: dict[str, dict[str, Any]] = {}
        for backend, declarations in servers.items():  # ``servers`` maps a backend name to the server declarations it serves
            if backend not in self._backends:
                raise ValueError(f"no backend named {backend!r} is configured")
            for declaration in declarations:
                server = _mcp().validate_server(declaration)
                for upstream, entry in server["tools"].items():
                    if entry["tool"] in self._routes:
                        raise ValueError(f"{entry['tool']} is routed twice")
                    self._routes[entry["tool"]] = {"declaration": dict(declaration), "server": server["name"], "upstream": upstream, "risk": entry["risk"], "backend": backend,
                                                   "family": entry["tool"].split(".", 1)[0] if entry.get("capability") else "mcp"}
        self._seen: OrderedDict[str, str] = OrderedDict()

    def routes(self) -> list[str]:
        return sorted(self._routes)

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def _refuse(self, reason: str, message: str, **details: Any) -> RouteRefused:
        self._record("upstream.refused", "runtime:upstream-router", {"reason": reason, **details})
        return RouteRefused(reason, message)

    def call(
        self,
        *,
        tenant: str,
        attested_agent: str,
        tool: str,
        arguments: Mapping[str, Any],
        upstream_definition: Mapping[str, Any],
        originator_permissions: Iterable[str],
        enabled_tools: Iterable[str],
        request_id: str,
        initiating_principal: str,
        approval_request_id: str | None = None,
        now: int,
    ) -> dict[str, Any]:
        """Route one portable Tool call; raises ``RouteRefused`` before anything is sent, ``UpstreamError``-free results otherwise."""
        if tenant != self.tenant:
            raise self._refuse("wrong-tenant", "this call is for another tenant", tool=tool)
        route = self._routes.get(tool) if isinstance(tool, str) else None
        if route is None:
            raise self._refuse("not-routed", "that Tool has no approved upstream route", tool=str(tool)[:80])
        if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
            raise self._refuse("bad-request-id", "a request id is required", tool=tool)
        if not isinstance(arguments, Mapping):
            raise self._refuse("bad-arguments", "arguments are an object", tool=tool)
        try:
            encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            raise self._refuse("bad-arguments", "arguments must be plain JSON", tool=tool) from None
        if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise self._refuse("bad-arguments", "arguments are too large", tool=tool)
        try:
            gate = _mcp().call_gate(route["declaration"], upstream_definition, self._approvals)
        except PermissionError as exc:
            raise self._refuse("not-approved", str(exc), tool=tool) from None
        if gate["tool"] != tool or upstream_definition.get("name") != route["upstream"]:
            raise self._refuse("not-approved", "the definition presented is not the one routed for this Tool", tool=tool)
        try:
            access = self._grants.resolve(
                attested_agent=attested_agent, family=route["family"], tool=tool, originator_permissions=originator_permissions,
                enabled_tools=enabled_tools, now=now,
            )
        except PermissionError as exc:
            raise self._refuse("no-grant", str(exc), tool=tool, agent=str(attested_agent)[:64]) from None
        if route["risk"] != "read" and approval_request_id != request_id:
            raise self._refuse("approval-required", f"{tool} needs approval for this exact request", tool=tool, risk=route["risk"])
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        previous = self._seen.get(request_id)
        if previous is not None and previous != digest:
            raise self._refuse("replay-mismatch", "that request id was already used with different arguments", tool=tool)
        self._seen[request_id] = digest
        self._seen.move_to_end(request_id)
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        key = hashlib.sha256(f"{self.tenant}\0{access['agent']}\0{tool}\0{request_id}".encode("utf-8")).hexdigest()
        request = {"operation": route["upstream"], "arguments": dict(arguments), "secretRef": access["secretRef"], "idempotencyKey": key,
                   "actingIdentity": access["actingIdentity"], "server": route["server"]}
        attribution = _grants().receipt_fields(access, initiating_principal=initiating_principal, request_id=request_id)
        base = {"tool": tool, "risk": route["risk"], "backend": route["backend"], "attribution": attribution, "idempotencyKey": key}
        try:
            raw = self._backends[route["backend"]].execute(request)
        except UpstreamError as exc:
            return self._failed(access, route, base, exc.code, now)
        except Exception:  # noqa: BLE001 - never echo what a backend said
            return self._failed(access, route, base, "outage", now)
        result = raw.get("result") if isinstance(raw, Mapping) else None
        try:
            size = len(json.dumps(result).encode("utf-8"))  # a result that is not plain JSON is rejected, never stringified
        except (TypeError, ValueError):
            size = MAX_RESULT_BYTES + 1
        if size > MAX_RESULT_BYTES:
            return self._failed(access, route, base, "rejected", now)
        receipt = raw.get("receipt") if isinstance(raw, Mapping) and isinstance(raw.get("receipt"), str) and _RECEIPT.match(raw["receipt"]) else None
        self._record("upstream.call", f"agent:{access['agent']}", {**{k: v for k, v in attribution.items() if k != "scopes"}, "tool": tool, "risk": route["risk"],
                                                                 "backend": route["backend"], "receipt": receipt, "outcome": "ok"})
        return {**base, "ok": True, "result": result, "untrusted": True, "receipt": receipt}

    def _failed(self, access: Mapping[str, Any], route: Mapping[str, Any], base: Mapping[str, Any], code: str, now: int) -> dict[str, Any]:
        if code == "unauthorized":
            try:  # the grant no longer works upstream: say so, and the owner is told through the existing connection notices
                self._grants.set_health(access["grantId"], "failed", by="runtime:upstream-router")
            except Exception:  # noqa: BLE001 - failing to record health must not hide the failure itself
                pass
        self._record("upstream.call", f"agent:{access['agent']}", {"tool": base["tool"], "risk": route["risk"], "backend": route["backend"], "grantId": access["grantId"],
                                                                 "outcome": "failed", "error": code})
        return {**base, "ok": False, "error": code, "untrusted": True}
