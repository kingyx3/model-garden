#!/usr/bin/env python3
"""Thin Lago usage-metering adapter.

Model Garden emits attributable usage facts; Lago owns aggregation, pricing, credits,
invoicing, subscription plans, and consumption/overage policy. This module intentionally
contains no pricing tables or invoice logic. The billing sync (``billing.py``) is its caller.
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

Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]

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


def _http_transport(method: str, url: str, headers: dict[str, str], payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    _validated_http_url(url, "Lago request URL")
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8"),
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
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        return exc.code, decoded if isinstance(decoded, dict) else {"error": raw or exc.reason}
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
    """Minimal Lago REST client: batched usage ingestion and billable-metric setup, nothing commercial."""

    BATCH = 100  # Lago's default LAGO_EVENTS_BATCH_MAX_LENGTH

    def __init__(self, api_key: str, *, base_url: str, transport: Transport = _http_transport) -> None:
        self.api_key = _require_text(api_key, "Lago API key")
        self.base_url = _validated_http_url(base_url, "Lago base URL")
        self.transport = transport

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        return self.transport(method, f"{self.base_url}{path}", headers, payload)

    def send_events(self, events: list[dict[str, Any]]) -> dict[str, int]:
        """Send ``build_usage_event(...)["event"]`` payloads in batches; returns how many were accepted and already present.

        Lago rejects a whole batch when any ``transaction_id`` already exists. An event Lago already holds is a retry of one sent
        before, so it is dropped and the rest of the batch is sent again; any other rejection raises and nothing is assumed sent.
        """
        accepted = duplicates = 0
        for start in range(0, len(events), self.BATCH):
            batch = list(events[start:start + self.BATCH])
            status, response = self._call("POST", "/api/v1/events/batch", {"events": batch})
            if status == 422:
                existing = _already_existing(response, len(batch))
                if existing:
                    batch = [event for index, event in enumerate(batch) if index not in existing]
                    duplicates += len(existing)
                    status, response = self._call("POST", "/api/v1/events/batch", {"events": batch}) if batch else (200, {})
            if not 200 <= status < 300:
                raise RuntimeError(f"Lago usage ingestion failed with HTTP {status}")
            accepted += len(batch)
        return {"accepted": accepted, "alreadyPresent": duplicates}

    def ensure_metric(self, definition: dict[str, Any]) -> str:
        """Create or update one billable metric by code; returns ``created``, ``updated`` or ``unchanged``."""
        code = _require_text(definition.get("code"), "billable metric code")
        status, current = self._call("GET", f"/api/v1/billable_metrics/{urllib.parse.quote(code, safe='')}")
        if status == 404:
            status, _ = self._call("POST", "/api/v1/billable_metrics", {"billable_metric": definition})
            outcome = "created"
        elif status == 200:
            if _same_metric(current.get("billable_metric") or {}, definition):
                return "unchanged"
            status, _ = self._call("PUT", f"/api/v1/billable_metrics/{urllib.parse.quote(code, safe='')}", {"billable_metric": definition})
            outcome = "updated"
        else:
            raise RuntimeError(f"Lago billable metric {code} could not be read (HTTP {status})")
        if not 200 <= status < 300:
            raise RuntimeError(f"Lago billable metric {code} could not be set up (HTTP {status})")
        return outcome


def _already_existing(response: dict[str, Any], size: int) -> set[int]:
    """Indexes Lago reported as an existing ``transaction_id``, or nothing when any error in the batch is something else."""
    details = response.get("error_details")
    if not isinstance(details, dict) or not details:
        return set()
    indexes = set()
    for key, errors in details.items():
        if not (isinstance(errors, dict) and errors == {"transaction_id": ["value_already_exist"]}):
            return set()
        try:
            index = int(key)
        except (TypeError, ValueError):
            return set()
        if not 0 <= index < size:
            return set()
        indexes.add(index)
    return indexes


def _same_metric(current: dict[str, Any], definition: dict[str, Any]) -> bool:
    def filters(value: Any) -> list[tuple[str, tuple[str, ...]]]:
        return sorted((str(item.get("key")), tuple(sorted(item.get("values") or []))) for item in value or [] if isinstance(item, dict))

    return all(current.get(key) == value for key, value in definition.items() if key != "filters") and \
        filters(current.get("filters")) == filters(definition.get("filters"))
