#!/usr/bin/env python3
"""Thin Lago usage-metering adapter.

Model Garden emits attributable usage facts; Lago owns aggregation, pricing, credits,
invoicing, subscription plans, and consumption/overage policy. This module intentionally
contains no pricing tables or invoice logic.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any]]]

_CANONICAL_ATTRIBUTION_KEYS = frozenset(
    {
        "client",
        "agent",
        "environment",
        "provider",
        "model",
        "usage_type",
        "quantity",
        "unit",
        "provider_cost_cents",
        "currency",
        "trace_id",
        "action_request_id",
        "cost_center",
        "billing_subscription_id",
        "shared_pool_id",
    }
)
_SENSITIVE_PROPERTY_KEYS = frozenset(
    {
        "prompt",
        "response",
        "transcript",
        "password",
        "api_key",
        "apikey",
        "authorization",
        "oauth_token",
        "access_token",
        "refresh_token",
        "credential",
        "secret",
    }
)
_ACTION_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


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


def _non_negative_number(value: int | float, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return value


def _normalized_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _reject_sensitive_properties(value: Any, *, path: str = "properties") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            if _normalized_key(key) in _SENSITIVE_PROPERTY_KEYS:
                raise ValueError(f"{path} must not contain sensitive field {key!r}")
            _reject_sensitive_properties(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_properties(child, path=f"{path}[{index}]")


def build_attribution_properties(
    *,
    client: str,
    agent: str,
    environment: str,
    provider: str,
    model: str | None = None,
    usage_type: str | None = None,
    quantity: int | float | None = None,
    unit: str | None = None,
    provider_cost_cents: int | float | None = None,
    currency: str | None = None,
    trace_id: str | None = None,
    action_request_id: str | None = None,
    cost_center: str | None = None,
    billing_subscription_id: str | None = None,
    shared_pool_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable Model Garden attribution dimensions carried in usage events.

    Commercial pricing remains outside the runtime. These fields support showback,
    provider-cost reconciliation, pooled subscriptions, and correlation to governed
    action audit records without sending prompts, responses, transcripts, or secrets.
    """
    properties: dict[str, Any] = {
        "client": _require_text(client, "client"),
        "agent": _require_text(agent, "agent"),
        "environment": _require_text(environment, "environment"),
        "provider": _require_text(provider, "provider"),
    }
    optional_text = {
        "model": model,
        "usage_type": usage_type,
        "unit": unit,
        "trace_id": trace_id,
        "cost_center": cost_center,
        "billing_subscription_id": billing_subscription_id,
        "shared_pool_id": shared_pool_id,
    }
    for key, value in optional_text.items():
        if value is not None:
            properties[key] = _require_text(value, key)
    if quantity is not None:
        properties["quantity"] = _non_negative_number(quantity, "quantity")
    if provider_cost_cents is not None:
        properties["provider_cost_cents"] = _non_negative_number(provider_cost_cents, "provider_cost_cents")
    if currency is not None:
        normalized_currency = _require_text(currency, "currency").upper()
        if not _CURRENCY.fullmatch(normalized_currency):
            raise ValueError("currency must be a three-letter ISO-style currency code")
        properties["currency"] = normalized_currency
    if action_request_id is not None:
        normalized_request_id = _require_text(action_request_id, "action_request_id")
        if not _ACTION_REQUEST_ID.fullmatch(normalized_request_id):
            raise ValueError("action_request_id must be a 64-character lowercase hexadecimal governed-action request id")
        properties["action_request_id"] = normalized_request_id
    if extra is not None:
        if not isinstance(extra, dict):
            raise ValueError("extra attribution properties must be a JSON object")
        overlap = sorted(_CANONICAL_ATTRIBUTION_KEYS.intersection(extra))
        if overlap:
            raise ValueError("extra attribution properties must not override canonical fields: " + ", ".join(overlap))
        properties.update(extra)
    _reject_sensitive_properties(properties)
    try:
        json.dumps(properties)
    except (TypeError, ValueError) as exc:
        raise ValueError("attribution properties must be JSON-serializable") from exc
    return properties


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
    if not math.isfinite(event["timestamp"]) or event["timestamp"] < 0:
        raise ValueError("timestamp must be a finite non-negative Unix timestamp")
    _reject_sensitive_properties(event["properties"])
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
