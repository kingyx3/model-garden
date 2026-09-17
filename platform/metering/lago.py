#!/usr/bin/env python3
"""Thin Lago usage-metering adapter.

Model Garden emits attributable usage facts; Lago owns aggregation, pricing, credits,
invoicing, and cost-plus/percentage commercial policy. This module intentionally contains
no pricing tables or invoice logic.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any]]]


def _validated_http_url(value: str, label: str) -> str:
    url = _require_text(value, label).rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"{label} must be an http(s) URL with a hostname")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not contain embedded credentials")
    return url


def _http_transport(method: str, url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    _validated_http_url(url, "Lago request URL")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        # nosec B310 -- URL scheme/hostname and embedded credentials are validated above.
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"error": raw or exc.reason}
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Lago request failed: {exc.reason}") from exc
    if not raw:
        return status, {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Lago returned a non-JSON response") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Lago returned an unexpected response shape")
    return status, decoded


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def build_usage_event(
    *,
    transaction_id: str,
    external_subscription_id: str,
    code: str,
    properties: dict[str, Any] | None = None,
    timestamp: int | float | None = None,
    precise_total_amount_cents: str | None = None,
) -> dict[str, Any]:
    """Build Lago's stable usage-event payload without applying pricing logic."""
    event: dict[str, Any] = {
        "transaction_id": _require_text(transaction_id, "transaction_id"),
        "external_subscription_id": _require_text(external_subscription_id, "external_subscription_id"),
        "code": _require_text(code, "code"),
        "timestamp": int(time.time()) if timestamp is None else timestamp,
        "properties": dict(properties or {}),
    }
    if isinstance(event["timestamp"], bool) or not isinstance(event["timestamp"], (int, float)):
        raise ValueError("timestamp must be a Unix timestamp number")
    if event["timestamp"] < 0:
        raise ValueError("timestamp must be non-negative")
    if precise_total_amount_cents is not None:
        event["precise_total_amount_cents"] = _require_text(precise_total_amount_cents, "precise_total_amount_cents")
    try:
        json.dumps(event)
    except (TypeError, ValueError) as exc:
        raise ValueError("usage event properties must be JSON-serializable") from exc
    return {"event": event}


class LagoClient:
    """Minimal Lago REST client for usage-event ingestion."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.getlago.com",
        transport: Transport = _http_transport,
    ) -> None:
        self.api_key = _require_text(api_key, "Lago API key")
        self.base_url = _validated_http_url(base_url, "Lago base URL")
        self.transport = transport

    def send_usage(
        self,
        *,
        transaction_id: str,
        external_subscription_id: str,
        code: str,
        properties: dict[str, Any] | None = None,
        timestamp: int | float | None = None,
        precise_total_amount_cents: str | None = None,
    ) -> dict[str, Any]:
        payload = build_usage_event(
            transaction_id=transaction_id,
            external_subscription_id=external_subscription_id,
            code=code,
            properties=properties,
            timestamp=timestamp,
            precise_total_amount_cents=precise_total_amount_cents,
        )
        status, response = self.transport(
            "POST",
            f"{self.base_url}/api/v1/events",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            payload,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(f"Lago usage ingestion failed with HTTP {status}")
        event = response.get("event")
        if event is not None and not isinstance(event, dict):
            raise RuntimeError("Lago usage ingestion returned an unexpected event shape")
        return response
