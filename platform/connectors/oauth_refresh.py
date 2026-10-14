"""Bounded in-memory Calendar OAuth refresh; credentials remain in the governed sidecar.

The environment's single secret:// calendar value can be an existing bearer token
or a JSON-encoded, operator-provisioned OAuth refresh credential. This module never
handles user consent or persists refreshed credentials. It is deliberately NOT a
substitute for an operator-approved durable token-vault/rotation lifecycle.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

GOOGLE_URL = "https://oauth2.googleapis.com/token"
TENANT = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, Any]]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Calendar token endpoint redirect refused")


def _exchange(url: str, fields: dict[str, str]) -> tuple[int, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.port is not None or parsed.fragment or parsed.query:
        raise ValueError("Unsupported OAuth token endpoint")
    if url != GOOGLE_URL and not (
        parsed.hostname == "login.microsoftonline.com"
        and re.fullmatch(r"/[0-9a-fA-F-]{36}/oauth2/v2[.]0/token", parsed.path)
        and TENANT.fullmatch(parsed.path.split("/")[1])
    ):
        raise ValueError("OAuth token endpoint not in approved Google/Microsoft allow-list")
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(32769)
            if len(raw) > 32768:
                raise RuntimeError("OAuth token response exceeds size limit")
            return response.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # OAuth bodies may contain account metadata or sensitive details. Never surface them.
        return exc.code, {}


class CalendarCredential:
    """Cache short-lived bearer tokens for one fixed provider; refresh safely before expiry."""

    def __init__(
        self,
        secret: str,
        provider: str,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if provider not in {"google", "microsoft"}:
            raise ValueError("Unsupported calendar OAuth provider")
        if not isinstance(secret, str) or not secret:
            raise ValueError("Calendar credential must be non-empty")
        self.provider = provider
        self._transport = transport or _exchange
        self._clock = clock
        self._lock = threading.Lock()
        self._static: str | None = None
        self._refresh: dict[str, str] | None = None
        self._token: str | None = None
        self._expires_at = 0.0

        if not secret.lstrip().startswith("{"):
            self._static = secret
            return
        try:
            payload = json.loads(secret)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid Calendar OAuth credential JSON") from exc
        if not isinstance(payload, dict) or payload.get("type") != "oauth2_refresh":
            raise ValueError("Calendar credential JSON must have type oauth2_refresh")
        if payload.get("provider") != provider:
            raise ValueError("Calendar OAuth credential provider mismatch")
        required = {"type", "provider", "client_id", "client_secret", "refresh_token"}
        if provider == "microsoft":
            required.add("tenant_id")
        if set(payload) != required:
            raise ValueError("Calendar OAuth credential fields do not match the provider contract")
        for key in required - {"type", "provider"}:
            if not isinstance(payload[key], str) or not payload[key].strip():
                raise ValueError(f"Calendar OAuth credential has invalid {key}")
        if provider == "microsoft" and not TENANT.fullmatch(payload["tenant_id"]):
            raise ValueError("Microsoft Calendar OAuth requires an approved tenant GUID")
        self._refresh = payload

    def token(self) -> str:
        if self._static is not None:
            return self._static
        with self._lock:
            now = self._clock()
            if self._token and self._expires_at - now > 60:
                return self._token
            assert self._refresh is not None
            url = (
                GOOGLE_URL if self.provider == "google"
                else f"https://login.microsoftonline.com/{self._refresh['tenant_id']}/oauth2/v2.0/token"
            )
            fields = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh["refresh_token"],
                "client_id": self._refresh["client_id"],
                "client_secret": self._refresh["client_secret"],
            }
            status, response = self._transport(url, fields)
            if not 200 <= status < 300:
                raise RuntimeError(f"Calendar OAuth refresh failed ({status}); reauthorization required")
            if not isinstance(response, dict):
                raise RuntimeError("Calendar OAuth token endpoint returned invalid data")
            token = response.get("access_token")
            lifetime = response.get("expires_in")
            if not isinstance(token, str) or not token or not isinstance(lifetime, int) or lifetime <= 0:
                raise RuntimeError("Calendar OAuth token endpoint returned invalid token or expiry")
            # Refresh on next use if provider TTL is <= one minute; never assume it is valid longer.
            self._token = token
            self._expires_at = self._clock() + lifetime
            return token
