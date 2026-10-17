"""The concrete network side of the governed upstream MCP seam: pinned HTTPS, a small MCP client, DNS and secret lookup (#87, #158, #159).

``mcp_executor.invoke`` is deliberately not an HTTP client. It asks the deployment for three things: a transport factory
``(url, pinned_ip, credential) -> context manager yielding a session``, a DNS resolver and a secret resolver. This module is those
three (plus the REST variant of the transport for a client's own API). It is installed in the governed sidecar only when the deployment turns upstream MCP on
(``MODEL_GARDEN_UPSTREAM_MCP=enabled``); without it the sidecar still refuses every upstream call.

- **Pinned address, normal TLS.** The connection goes to the IP the endpoint guard qualified, while the certificate is verified for the
  declared host name (the default verifying SSL context; hostname checking cannot be switched off here). The name is never resolved
  again after qualification, so DNS rebinding cannot move the connection.
- **No redirects, bounded.** A 3xx is an error, never followed. Request and response sizes, the number of listed tools and the time
  per call are bounded; a response over the bound is an error rather than a truncated answer.
- **A minimal MCP client, not a general fetch.** It speaks only the streamable-HTTP MCP methods ``initialize``, ``tools/list`` and
  ``tools/call`` to the single declared server. It exposes ``list_tools()`` and ``call_tool(name, arguments)`` and nothing else.
- **Credentials stay out of everything.** The bearer credential goes in the ``Authorization`` header only. No exception or return value
  built here carries the header, the URL's path or any upstream body; errors name the HTTP status or a JSON-RPC code only.
- **A client's own REST API, the same way.** ``RestApiSession`` serves a connection whose declaration carries a ``custom_api``
  definition: it lists only the declared operations and sends only the request ``custom_api.build_request`` builds for an approved one,
  with the same pinning, no-redirect and size rules. A 4xx other than 401/403 is an operation failure; nothing upstream is echoed.
- **Secrets by reference.** ``EnvSecretResolver`` maps a ``secret://`` reference to an environment variable the deployment injected into
  this service alone (``MODEL_GARDEN_SECRET_<digest>``). The Hermes process has no such variable.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import json
import pathlib
import re
import socket
import ssl
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
MAX_REQUEST_BYTES = 262_144
MAX_RESPONSE_BYTES = 1_048_576
MAX_LIST_PAGES = 5
DEFAULT_TIMEOUT = 20
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SESSION_ID = re.compile(r"^[\x21-\x7e]{1,200}$")


class UpstreamTransportError(RuntimeError):
    """The upstream could not be used; the message never carries a URL path, header or body."""


def secret_env_name(reference: str) -> str:
    """The environment variable the deployment injects for a ``secret://`` reference (stable, shell-safe)."""
    return "MODEL_GARDEN_SECRET_" + hashlib.sha256(reference.encode("utf-8")).hexdigest()[:24].upper()


class EnvSecretResolver:
    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def __call__(self, reference: str) -> str:
        value = self._environ.get(secret_env_name(reference), "")
        if not value:
            raise UpstreamTransportError("credential unavailable")
        return value


def system_dns_resolver(host: str) -> list[str]:
    """Every address ``host`` resolves to; the endpoint guard decides which are acceptable and pins one."""
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except OSError:
        raise UpstreamTransportError("host did not resolve") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Dials ``pinned_ip`` but verifies the certificate for ``host`` (and sends it as SNI and ``Host``)."""

    def __init__(self, host: str, pinned_ip: str, port: int = 443, timeout: float = DEFAULT_TIMEOUT,
                 context: ssl.SSLContext | None = None) -> None:
        super().__init__(host, port, timeout=timeout, context=context or ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def _split(url: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"https://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::(\d+))?(/[^?#\s]*)?", url)
    if match is None:
        raise UpstreamTransportError("not an https endpoint")
    return match.group(1).strip("[]"), int(match.group(2) or 443), match.group(3) or "/"


Connect = Callable[[str, str, int, float], Any]


class McpHttpSession:
    """One streamable-HTTP MCP session to the declared server. Use as a context manager."""

    def __init__(self, url: str, pinned_ip: str, credential: str, *, timeout: float = DEFAULT_TIMEOUT, connect: Connect | None = None) -> None:
        self._host, self._port, self._path = _split(url)
        self._pinned, self._credential, self._timeout = pinned_ip, credential, timeout
        self._connect = connect or (lambda host, ip, port, timeout: PinnedHTTPSConnection(host, ip, port, timeout))
        self._session_id: str | None = None
        self._next_id = 0

    # -- plumbing ---------------------------------------------------------------------------------

    def _post(self, message: Mapping[str, Any], *, expect_reply: bool) -> Any:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise UpstreamTransportError("request too large")
        headers = {
            "Authorization": f"Bearer {self._credential}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        connection = self._connect(self._host, self._pinned, self._port, self._timeout)
        try:
            connection.request("POST", self._path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status, content_type = response.status, (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            session = response.getheader("Mcp-Session-Id")
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise UpstreamTransportError("upstream unreachable") from None
        finally:
            connection.close()
        if session and _SESSION_ID.match(session):
            self._session_id = session
        if 300 <= status < 400:
            raise UpstreamTransportError("upstream redirected; redirects are never followed")
        if status in (401, 403):
            raise UpstreamTransportError(f"upstream refused the credential ({status})")
        if not 200 <= status < 300:
            raise UpstreamTransportError(f"upstream answered HTTP {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise UpstreamTransportError("upstream response too large")
        if not expect_reply:
            return None
        return self._reply(raw, content_type, message.get("id"))

    @staticmethod
    def _reply(raw: bytes, content_type: str, request_id: Any) -> Any:
        candidates: list[Any] = []
        try:
            if content_type == "text/event-stream":
                for block in raw.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(line[5:].lstrip() for line in block.split("\n") if line.startswith("data:"))
                    if data:
                        candidates.append(json.loads(data))
            else:
                document = json.loads(raw)
                candidates.extend(document if isinstance(document, list) else [document])
        except (ValueError, UnicodeDecodeError):
            raise UpstreamTransportError("upstream sent an unreadable reply") from None
        for item in candidates:
            if isinstance(item, dict) and item.get("id") == request_id and ("result" in item or "error" in item):
                if "error" in item:
                    code = item["error"].get("code") if isinstance(item["error"], dict) else None
                    raise UpstreamTransportError(f"upstream MCP error ({code if isinstance(code, int) else 'unknown'})")
                return item["result"]
        raise UpstreamTransportError("upstream sent no matching reply")

    def _call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        return self._post(message, expect_reply=True)

    # -- session ----------------------------------------------------------------------------------

    def __enter__(self) -> "McpHttpSession":
        result = self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "model-garden-gateway", "version": "1"},
        })
        if not isinstance(result, dict):
            raise UpstreamTransportError("upstream did not initialise")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
        return self

    def __exit__(self, *exc: Any) -> None:
        if not self._session_id:
            return
        connection = self._connect(self._host, self._pinned, self._port, 5)
        try:  # courtesy close; the session also expires upstream
            connection.request("DELETE", self._path, headers={"Authorization": f"Bearer {self._credential}", "Mcp-Session-Id": self._session_id})
            connection.getresponse().read(1)
        except (OSError, http.client.HTTPException, ssl.SSLError):
            pass
        finally:
            connection.close()

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            result = self._call("tools/list", {"cursor": cursor} if cursor else None)
            page = result.get("tools") if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise UpstreamTransportError("upstream tool list is malformed")
            tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise UpstreamTransportError("upstream tool list is unbounded")

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = self._call("tools/call", {"name": name, "arguments": dict(arguments)})
        if not isinstance(result, dict):
            raise UpstreamTransportError("upstream result is malformed")
        structured = result.get("structuredContent")
        identifier = structured.get("id") if isinstance(structured, dict) else None
        if "externalObjectRef" not in result and isinstance(identifier, (str, int)) and _SAFE_ID.match(str(identifier)):
            result = {**result, "externalObjectRef": str(identifier)}  # an opaque provider id only; the ledger never keeps bodies
        return result


class RestApiSession:
    """A client's own REST API presented to ``mcp_executor.invoke`` as a session over its declared, approved operations.

    ``list_tools`` is the reviewed definition's operations (``custom_api.operation_tools``), so the executor checks the same pins it
    checks for an MCP server. ``call_tool`` sends only the request ``custom_api.build_request`` builds for one approved operation, to the
    pinned address, with the credential added according to the definition's authentication type. There is no other request.
    """

    def __init__(self, definition: Mapping[str, Any], pinned_ip: str, credential: str, approvals: Any, *, connect: Connect | None = None) -> None:
        self._definition, self._approvals = definition, approvals
        self._api = _custom_api()
        self._host, self._port, _ = _split(self._api.validate_definition(definition)["baseUrl"])
        self._pinned, self._credential = pinned_ip, credential
        self._timeout = float(self._api.LIMITS["timeoutSeconds"])
        self._connect = connect or (lambda host, ip, port, timeout: PinnedHTTPSConnection(host, ip, port, timeout))

    def __enter__(self) -> "RestApiSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def list_tools(self) -> list[dict[str, Any]]:
        return self._api.operation_tools(self._definition)

    def _auth_headers(self) -> dict[str, str]:
        auth = self._api.validate_definition(self._definition)["auth"]
        if auth["type"] == "bearer":
            return {"Authorization": f"Bearer {self._credential}"}
        if auth["type"] == "header":
            return {auth["header"]: self._credential}
        if auth["type"] == "basic":  # the stored credential is "user:password"
            return {"Authorization": "Basic " + base64.b64encode(self._credential.encode("utf-8")).decode("ascii")}
        return {}

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = self._api.build_request(self._definition, name, arguments, self._approvals)
        except ValueError:
            raise UpstreamTransportError("request refused by the API definition") from None
        host, port, target = _split(request["url"].split("?", 1)[0])
        if (host, port) != (self._host, self._port):
            raise UpstreamTransportError("request left the declared API")
        if "?" in request["url"]:
            target += "?" + request["url"].split("?", 1)[1]
        headers = {"Accept": "application/json", **self._auth_headers()}
        body = None
        if "body" in request:
            body = json.dumps(request["body"], separators=(",", ":"), sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = self._connect(self._host, self._pinned, self._port, self._timeout)
        try:
            connection.request(request["method"], target, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status, content_type = response.status, (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise UpstreamTransportError("upstream unreachable") from None
        finally:
            connection.close()
        if 300 <= status < 400:
            raise UpstreamTransportError("upstream redirected; redirects are never followed")
        if status in (401, 403):
            raise UpstreamTransportError(f"upstream refused the credential ({status})")
        if status == 429 or status >= 500:
            raise UpstreamTransportError(f"upstream answered HTTP {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise UpstreamTransportError("upstream response too large")
        if 400 <= status < 500:  # the API rejected this request: an operation-level failure, with no upstream text
            return {"isError": True, "status": status}
        if not raw.strip():
            document: Any = {}
        elif content_type == "application/json" or content_type.endswith("+json"):
            try:
                document = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise UpstreamTransportError("upstream sent an unreadable reply") from None
        else:
            raise UpstreamTransportError("upstream reply is not JSON")
        result: dict[str, Any] = {"isError": False, "status": status, "structuredContent": document if isinstance(document, dict) else {"items": document}}
        identifier = document.get("id") if isinstance(document, dict) else None
        if isinstance(identifier, (str, int)) and not isinstance(identifier, bool) and _SAFE_ID.match(str(identifier)):
            result["externalObjectRef"] = str(identifier)
        return result


def _custom_api():
    name = "model_garden_upstream_custom_api"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parents[1] / "core" / "custom_api.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def rest_transport_factory(definition: Mapping[str, Any], pinned_ip: str, credential: str, approvals: Any) -> RestApiSession:
    """The transport the governed sidecar uses for a connection whose declaration carries a client's own REST ``api``."""
    return RestApiSession(definition, pinned_ip, credential, approvals)


def transport_factory(url: str, pinned_ip: str, credential: str) -> McpHttpSession:
    """The ``transport_factory`` ``mcp_executor.invoke`` expects."""
    return McpHttpSession(url, pinned_ip, credential)


def install(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Keyword arguments for ``GovernedToolRuntime`` when the deployment turned upstream MCP on, else ``None`` (everything stays refused)."""
    if environ.get("MODEL_GARDEN_UPSTREAM_MCP", "").strip().lower() != "enabled":
        return None
    return {"upstream_transport": transport_factory, "upstream_rest_transport": rest_transport_factory,
            "upstream_dns_resolver": system_dns_resolver, "upstream_secret_resolver": EnvSecretResolver(environ)}
