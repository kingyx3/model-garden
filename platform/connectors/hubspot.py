"""HubSpot CRM connector for the portable ``crm.*`` Tools, with a field allow-list and no merging.

Implements ``crm.read``, ``crm.update``, ``crm.create`` and ``crm.log-interaction`` against the HubSpot CRM v3 API behind
an injected transport, for contacts, companies and deals. It is built to sit behind the governed boundary:

- **Field allow-list.** The connection is configured with the properties each object type may expose
  (``allowed_fields``) and, separately, may change (``writable_fields``, a subset). A property outside the list is never
  returned to the model even if HubSpot sends it, and a write naming one is refused before any request. With no
  ``writable_fields`` the connection is read-only for update and create.
- **No merging.** ``crm.create`` never merges or updates an existing record: a HubSpot conflict is reported as a refusal
  that does not reveal the existing record.
- **Duplicate-safe interaction logging.** ``crm.log-interaction`` writes a note associated with the record and embeds a
  content-derived marker token; before writing it searches for that marker, so a re-delivered interaction returns the
  existing note and writes nothing. The note holds the summary and a *reference* to the transcript, never its text; card
  numbers are redacted.
- **Errors a sync ledger can use.** Failures raise ``HubSpotError`` carrying a ``code`` (``rate-limited``, ``outage``,
  ``unauthorized`` or ``rejected``) that matches ``platform/core/crm_sync.py``.

Authentication (a private-app or OAuth access token), consent, scopes granted, the per-coworker identity (#129) and secret
storage are outside this module: the caller injects a token at runtime. HubSpot's default association type ids for notes
are used and still need confirming in a live test. Status: credential-free tests only (a fake transport); the sidecar
does not serve these Tools for HubSpot and nothing here has had a live HubSpot proof.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

HUBSPOT_API = "https://api.hubapi.com"
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]
OBJECT_PATHS = {"contact": "contacts", "company": "companies", "deal": "deals"}
NOTE_ASSOCIATION = {"contact": 202, "company": 190, "deal": 214}  # HubSpot-defined note-to-object association type ids
SCOPES = {
    "crm.read": ["crm.objects.contacts.read", "crm.objects.companies.read", "crm.objects.deals.read"],
    "crm.update": ["crm.objects.contacts.write", "crm.objects.companies.write", "crm.objects.deals.write"],
    "crm.create": ["crm.objects.contacts.write", "crm.objects.companies.write", "crm.objects.deals.write"],
    "crm.log-interaction": ["crm.objects.contacts.write"],
}
INTERACTION_TYPES = ("call", "email", "chat")
DIRECTIONS = ("inbound", "outbound")
MAX_VALUE = 500
MAX_SUMMARY = 2000
MAX_FIELDS = 30
MAX_RECORDS = 10
_PROPERTY = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_ID = re.compile(r"^[0-9]{1,20}$")
_REF = re.compile(r"^[A-Za-z0-9:._/#@-]{1,256}$")
_SHORT = re.compile(r"^[A-Za-z0-9 ._:/-]{1,100}$")
_CODES = {429: "rate-limited", 401: "unauthorized", 403: "unauthorized"}


class HubSpotError(RuntimeError):
    """A HubSpot failure; ``code`` is one of ``rate-limited``, ``outage``, ``unauthorized`` or ``rejected``."""

    def __init__(self, message: str, code: str, status: int) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _contacts_module():
    name = "model_garden_hubspot_contacts"
    if name not in sys.modules:
        path = pathlib.Path(__file__).resolve().parents[1] / "core" / "contacts.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("HubSpot redirect refused")


def _urllib_transport(method: str, url: str, headers: dict[str, str], payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.hubapi.com" or parsed.port is not None:
        raise ValueError("HubSpot transport only permits the fixed HTTPS HubSpot API host")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        # nosec B310 -- scheme/host are constrained and redirects are denied.
        with opener.open(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"message": raw or str(exc)}


def _fields(config: Mapping[str, Sequence[str]], label: str) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for object_type, names in config.items():
        if object_type not in OBJECT_PATHS:
            raise ValueError(f"{label}: object type must be one of {', '.join(OBJECT_PATHS)}")
        listed = list(names)
        if not listed or len(listed) > 100 or not all(isinstance(name, str) and _PROPERTY.match(name) for name in listed):
            raise ValueError(f"{label}.{object_type}: list 1 to 100 HubSpot property names")
        result[object_type] = frozenset(listed)
    return result


class HubSpotClient:
    def __init__(
        self,
        access_token: str,
        *,
        allowed_fields: Mapping[str, Sequence[str]],
        writable_fields: Mapping[str, Sequence[str]] | None = None,
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("HubSpot access token must be supplied at runtime")
        self._allowed = _fields(allowed_fields, "allowed_fields")
        if not self._allowed:
            raise ValueError("allowed_fields must list the properties each object type may expose; there is no allow-all")
        self._writable = _fields(writable_fields or {}, "writable_fields")
        for object_type, names in self._writable.items():
            if object_type not in self._allowed or not names <= self._allowed[object_type]:
                raise ValueError(f"writable_fields.{object_type} must be a subset of allowed_fields.{object_type}")
        self._token = access_token
        self._transport = transport or _urllib_transport
        self._redact = _contacts_module().redact_cards

    # -- plumbing -----------------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json"}
        status, body = self._transport(method, f"{HUBSPOT_API}{path}", headers, payload)
        if 200 <= status < 300:
            if not isinstance(body, dict):
                raise HubSpotError("HubSpot returned a non-object response", "outage", status)
            return body
        code = _CODES.get(status) or ("outage" if status >= 500 else "rejected")
        if status == 409:
            raise HubSpotError("HubSpot refused: a record with this identity already exists; records are never merged", "rejected", status)
        raise HubSpotError(f"HubSpot request failed ({status}): {code}", code, status)

    def _object(self, arguments: Mapping[str, Any]) -> str:
        object_type = arguments.get("objectType", "contact")
        if object_type not in OBJECT_PATHS or object_type not in self._allowed:
            raise ValueError(f"objectType must be one of the configured types: {', '.join(sorted(self._allowed))}")
        return object_type

    @staticmethod
    def _object_id(value: Any) -> str:
        if not isinstance(value, str) or not _ID.match(value):
            raise ValueError("objectId must be a HubSpot numeric record id")
        return value

    def _shape(self, object_type: str, record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise HubSpotError("HubSpot returned an invalid record", "outage", 200)
        object_id = self._object_id(str(record.get("id", "")))
        allowed = self._allowed[object_type]
        properties = record.get("properties") if isinstance(record.get("properties"), dict) else {}
        fields = {name: str(value)[:MAX_VALUE] for name, value in properties.items() if name in allowed and value is not None}
        return {"objectType": object_type, "objectId": object_id, "fields": fields, "externalObjectRef": f"hubspot:{object_type}/{object_id}"}

    def _writes(self, object_type: str, fields: Any) -> dict[str, str]:
        if not isinstance(fields, dict) or not fields or len(fields) > MAX_FIELDS:
            raise ValueError(f"fields must be an object of 1 to {MAX_FIELDS} properties")
        writable = self._writable.get(object_type, frozenset())
        refused = sorted(name for name in fields if name not in writable)
        if refused:
            raise ValueError(f"these properties may not be written on {object_type}: {', '.join(str(name) for name in refused)}")
        values: dict[str, str] = {}
        for name, value in fields.items():
            if isinstance(value, (dict, list)) or value is None:
                raise ValueError(f"{name} must be a plain text or number value")
            text = self._redact(str(value))
            if len(text) > MAX_VALUE:
                raise ValueError(f"{name} is at most {MAX_VALUE} characters")
            values[name] = text
        return values

    # -- portable Tools -----------------------------------------------------------------

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("crm.read arguments must be a JSON object")
        object_type = self._object(arguments)
        path = OBJECT_PATHS[object_type]
        properties = sorted(self._allowed[object_type])
        object_id, query = arguments.get("objectId"), arguments.get("query")
        if (object_id is None) == (query is None):
            raise ValueError("crm.read needs exactly one of objectId or query")
        if object_id is not None:
            record = self._request("GET", f"/crm/v3/objects/{path}/{self._object_id(object_id)}?properties={','.join(properties)}")
            return {"provider": "hubspot", "records": [self._shape(object_type, record)]}
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("query must be a non-empty string of at most 200 characters")
        found = self._request("POST", f"/crm/v3/objects/{path}/search", {"query": query.strip(), "limit": MAX_RECORDS, "properties": properties})
        return {"provider": "hubspot", "records": [self._shape(object_type, item) for item in (found.get("results") or [])[:MAX_RECORDS]]}

    def update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("crm.update arguments must be a JSON object")
        object_type = self._object(arguments)
        object_id = self._object_id(arguments.get("objectId"))
        properties = self._writes(object_type, arguments.get("fields"))
        record = self._request("PATCH", f"/crm/v3/objects/{OBJECT_PATHS[object_type]}/{object_id}", {"properties": properties})
        return {"provider": "hubspot", "objectId": object_id, "externalObjectRef": f"hubspot:{object_type}/{object_id}",
                "updated": sorted(properties), "id": record.get("id", object_id)}

    def create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Create a record; a conflict is refused, never merged or updated."""
        if not isinstance(arguments, dict):
            raise ValueError("crm.create arguments must be a JSON object")
        object_type = self._object(arguments)
        properties = self._writes(object_type, arguments.get("fields"))
        record = self._request("POST", f"/crm/v3/objects/{OBJECT_PATHS[object_type]}", {"properties": properties})
        object_id = self._object_id(str(record.get("id", "")))
        return {"provider": "hubspot", "objectId": object_id, "externalObjectRef": f"hubspot:{object_type}/{object_id}"}

    def log_interaction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("crm.log-interaction arguments must be a JSON object")
        object_type = self._object(arguments)
        object_id = self._object_id(arguments.get("objectId"))
        kind, direction = arguments.get("interactionType"), arguments.get("direction")
        if kind not in INTERACTION_TYPES or direction not in DIRECTIONS:
            raise ValueError("interactionType must be call, email or chat and direction inbound or outbound")
        channel = arguments.get("channel")
        if not isinstance(channel, str) or not _SHORT.match(channel):
            raise ValueError("channel must be a short label such as voice or email")
        try:
            moment = dt.datetime.fromisoformat(str(arguments.get("timestamp")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be an RFC3339 time") from exc
        if moment.tzinfo is None:
            raise ValueError("timestamp must include a timezone offset")
        timestamp = moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY:
            raise ValueError(f"summary must be a non-empty string of at most {MAX_SUMMARY} characters")
        outcome, reference = arguments.get("outcome"), arguments.get("transcriptRef")
        if outcome is not None and (not isinstance(outcome, str) or not _SHORT.match(outcome)):
            raise ValueError("outcome must be a short label")
        if reference is not None and (not isinstance(reference, str) or not _REF.match(reference)):
            raise ValueError("transcriptRef must be a reference to the stored transcript, never its text")
        cleaned = self._redact(summary.strip())
        material = json.dumps([object_type, object_id, timestamp, channel, direction, kind, cleaned], separators=(",", ":"))
        marker = "mgkey" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        existing = self._request("POST", "/crm/v3/objects/notes/search", {
            "filterGroups": [{"filters": [{"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN", "value": marker}]}],
            "limit": 1, "properties": ["hs_timestamp"],
        })
        found = (existing.get("results") or [])
        if found and isinstance(found[0], dict) and found[0].get("id"):
            note_id = self._object_id(str(found[0]["id"]))
            return {"provider": "hubspot", "interactionId": note_id, "externalObjectRef": f"hubspot:note/{note_id}", "duplicate": True}
        lines = [f"[{kind} / {direction} via {channel}] {cleaned}"]
        if outcome:
            lines.append(f"Outcome: {outcome}")
        if reference:
            lines.append(f"Transcript reference: {reference}")
        lines.append(f"Model Garden marker: {marker}")
        created = self._request("POST", "/crm/v3/objects/notes", {
            "properties": {"hs_timestamp": timestamp, "hs_note_body": "\n".join(lines)},
            "associations": [{"to": {"id": object_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_ASSOCIATION[object_type]}]}],
        })
        note_id = self._object_id(str(created.get("id", "")))
        return {"provider": "hubspot", "interactionId": note_id, "externalObjectRef": f"hubspot:note/{note_id}", "duplicate": False}

    def executors(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {"crm.read": self.read, "crm.update": self.update, "crm.create": self.create, "crm.log-interaction": self.log_interaction}
