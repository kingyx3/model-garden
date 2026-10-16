"""A constrained HTTPS transport shared by the connectors that do not need anything special.

Every connector takes an injected ``transport(method, url, headers, payload) -> (status, body)`` so tests never touch a
network. The default transport here is deliberately narrow: HTTPS only, no port, a host that matches the provider's pattern,
no redirects (a redirect could carry the bearer token to another host) and a 15 second timeout. It has no credential of its
own; the caller supplies headers.

Payloads: ``None``; a ``dict`` (sent as JSON); ``{"_text": str, "_contentType": str}`` (sent as raw text, for an upload); or
``{"_multipart": {"metadata": dict, "content": str, "contentType": str}}`` (a Google-style ``multipart/related`` upload).
Responses: the parsed JSON object, ``{}`` for an empty body, or ``{"_text": raw}`` when the body is not JSON (for example
exported document text).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

Transport = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect refused")


def _body(payload: Any, headers: dict[str, str]) -> bytes | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if "_text" in payload:
        headers["Content-Type"] = str(payload.get("_contentType") or "text/plain")
        return str(payload["_text"]).encode("utf-8")
    if "_multipart" in payload:
        part = payload["_multipart"]
        boundary = "mg" + uuid.uuid4().hex
        headers["Content-Type"] = f"multipart/related; boundary={boundary}"
        pieces = [
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(part['metadata'])}\r\n",
            f"--{boundary}\r\nContent-Type: {part.get('contentType') or 'text/plain'}; charset=UTF-8\r\n\r\n{part['content']}\r\n",
            f"--{boundary}--",
        ]
        return "".join(pieces).encode("utf-8")
    headers.setdefault("Content-Type", "application/json")
    return json.dumps(payload).encode("utf-8")


def make_transport(label: str, host: re.Pattern[str]) -> Transport:
    """A transport that only reaches an HTTPS host matching ``host`` (no port, no redirects)."""

    def transport(method: str, url: str, headers: dict[str, str], payload: Any) -> tuple[int, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.port is not None or not host.fullmatch(parsed.hostname or ""):
            raise ValueError(f"{label} transport only permits the fixed HTTPS {label} host")
        sent = dict(headers)
        data = _body(payload, sent)
        request = urllib.request.Request(url, data=data, headers=sent, method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            # nosec B310 -- scheme and host are constrained above and redirects are denied.
            with opener.open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError(f"{label} response is too large")
                return response.status, _parse(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            return exc.code, _parse(raw) if raw else {"message": str(exc)}

    return transport


def _parse(raw: str) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_text": raw}
