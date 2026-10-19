"""Send one checked request to a client's own REST API from the governed sidecar (#159, #89).

``platform/core/custom_api.py`` turns a declared, approved operation into a checked request (method, URL, query, JSON body, limits)
and holds no HTTP client. This is the sender it describes. It is not a general fetch tool: the sidecar passes it only a request
that ``build_request`` produced, to the one host the definition declares.

- **Pinned and verified.** The caller resolves the host through the endpoint guard and passes one qualified public address; the
  connection dials that address and verifies the certificate for the declared name (the same connection class as the MCP
  transport), so DNS cannot move it after the check.
- **No redirects, bounded sizes.** A 3xx is an error and a response over ``maxResponseBytes`` is refused.
- **Credential in one header.** ``bearer`` sends ``Authorization: Bearer``, ``header`` sends the declared header, ``basic`` sends
  ``Authorization: Basic`` from a ``user:password`` secret. The value is resolved from the sidecar's own environment and never
  appears in an error, a result or the ledger.
- **Errors say the status.** Never the body, URL path or headers, which can echo customer data.
- **Results are untrusted data.** A JSON body is returned parsed and bounded; anything else is returned as short text marked as such.
"""
from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import pathlib
import ssl
import sys
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

MAX_TEXT = 4000
USER_AGENT = "model-garden-governed-sidecar"


def _upstream():
    name = "model_garden_custom_api_upstream_http"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("upstream_http.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class CustomApiTransportError(RuntimeError):
    """The request could not be completed; the message holds a status or a reason, never data from the API or the request."""


Connect = Callable[[str, str, int, float], Any]


def _headers(auth: Mapping[str, Any], credential: str, has_body: bool) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if has_body:
        headers["Content-Type"] = "application/json"
    kind = auth.get("type")
    if kind == "bearer":
        headers["Authorization"] = f"Bearer {credential}"
    elif kind == "header":
        headers[str(auth["header"])] = credential
    elif kind == "basic":
        headers["Authorization"] = "Basic " + base64.b64encode(credential.encode("utf-8")).decode("ascii")
    elif kind != "none":
        raise CustomApiTransportError("unsupported authentication type")
    return headers


def send(request: Mapping[str, Any], *, auth: Mapping[str, Any], credential: str | None, pinned_ip: str, connect: Connect | None = None) -> dict[str, Any]:
    """Send ``request`` (from ``custom_api.build_request``) to ``pinned_ip`` and return ``{"status", "body" | "text"}``."""
    kind = auth.get("type")
    if kind != "none" and not credential:
        raise CustomApiTransportError("the API credential is unavailable")
    parts = urlsplit(str(request["url"]))
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise CustomApiTransportError("not an https endpoint")
    limits = request["limits"]
    body = json.dumps(request["body"], separators=(",", ":"), sort_keys=True).encode("utf-8") if "body" in request else None
    if body is not None and len(body) > limits["maxRequestBytes"]:
        raise CustomApiTransportError("the request body is too large")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    opener = connect or (lambda host, ip, port, timeout: _upstream().PinnedHTTPSConnection(host, ip, port, timeout))
    connection = opener(parts.hostname, pinned_ip, parts.port or 443, float(limits["timeoutSeconds"]))
    try:
        connection.request(request["method"], target, body=body, headers=_headers(auth, credential or "", body is not None))
        response = connection.getresponse()
        raw = response.read(limits["maxResponseBytes"] + 1)
        status, content_type = response.status, (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
    except (OSError, http.client.HTTPException, ssl.SSLError):
        raise CustomApiTransportError("the API was unreachable") from None
    finally:
        connection.close()
    if 300 <= status < 400:
        raise CustomApiTransportError("the API redirected; redirects are never followed")
    if status in (401, 403):
        raise CustomApiTransportError(f"the API refused the credential ({status})")
    if not 200 <= status < 300:
        raise CustomApiTransportError(f"the API answered HTTP {status}")
    if len(raw) > limits["maxResponseBytes"]:
        raise CustomApiTransportError("the API response is too large")
    if not raw:
        return {"status": status}
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            return {"status": status, "body": json.loads(raw)}
        except ValueError:
            raise CustomApiTransportError("the API returned invalid JSON") from None
    return {"status": status, "text": raw.decode("utf-8", errors="replace")[:MAX_TEXT], "untrustedText": True}


def install(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Keyword arguments for ``GovernedToolRuntime`` when the deployment declared custom APIs, else ``None`` (nothing is offered)."""
    if environ.get("MODEL_GARDEN_CUSTOM_API", "").strip().lower() != "enabled":
        return None
    path = pathlib.Path(environ.get("MODEL_GARDEN_CUSTOM_API_DEFINITIONS", "/opt/profile-seed/.modelgarden/custom-apis.json"))
    definitions = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(definitions, list):
        raise ValueError("custom API definitions must be a list")
    upstream = _upstream()
    return {"custom_api": {"definitions": definitions, "send": send, "dns_resolver": upstream.system_dns_resolver,
                            "secret_resolver": upstream.EnvSecretResolver(environ)}}
