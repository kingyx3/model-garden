"""Salesforce CRM connector for the portable ``crm.*`` Tools, with a field allow-list and no merging.

Implements ``crm.read``, ``crm.update``, ``crm.create`` and ``crm.log-interaction`` against the Salesforce REST API behind an
injected transport, for the same portable object types as the HubSpot adapter (``contact`` is Contact, ``company`` is Account
and ``deal`` is Opportunity), so one Agent and one Skill work with either CRM. It is built to sit behind the governed boundary:

- **Field allow-list.** The connection is configured with the fields each object type may expose (``allowed_fields``) and,
  separately, may change (``writable_fields``, a subset). A field outside the list is never requested, never returned to the
  model even if Salesforce sends it, and a write naming one is refused before any request. With no ``writable_fields`` the
  connection is read-only for update and create.
- **No merging.** ``crm.create`` sends ``Sforce-Duplicate-Rule-Header: allowSave=false`` so Salesforce's own duplicate rules
  refuse a duplicate; the refusal is reported without revealing the existing record. (A org with no duplicate rule configured
  will not refuse; the connection cannot add rules.)
- **Duplicate-safe interaction logging.** ``crm.log-interaction`` records a completed Task associated with the record. A
  content-derived marker is placed at the end of the Task *subject* (a filterable field; the description is not) and searched
  for before writing, so a re-delivered interaction returns the existing Task and writes nothing. The Task holds the summary and
  a *reference* to the transcript, never its text; card numbers are redacted.
- **Errors a sync ledger can use.** Failures raise ``SalesforceError`` carrying a ``code`` (``rate-limited``, ``outage``,
  ``unauthorized`` or ``rejected``) that matches ``platform/core/crm_sync.py``. Salesforce reports an exhausted API limit as a
  403 with ``REQUEST_LIMIT_EXCEEDED``; that is ``rate-limited``, not ``unauthorized``. Error text is never echoed (it can hold
  field values); only Salesforce's error code is kept.
- **Pinned instance.** The access token is only ever sent to the one configured ``https://<org>.my.salesforce.com`` instance.

Authentication (an OAuth access token for the coworker's own integration user, #129), consent, the connected app's scopes and
secret storage are outside this module: the caller injects a token at runtime. Salesforce's OAuth ``api`` scope is coarse, so
least privilege here rests on the field allow-list and on the integration user's profile and permission sets, which the owner
configures in Salesforce. Status: credential-free tests only (a fake transport); the sidecar does not serve these Tools for
Salesforce and nothing here has had a live Salesforce proof.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any

API_VERSION = "v60.0"
OBJECT_NAMES = {"contact": "Contact", "company": "Account", "deal": "Opportunity"}
SCOPES = {tool: ["api"] for tool in ("crm.read", "crm.update", "crm.create", "crm.log-interaction")}
INTERACTION_TYPES = ("call", "email", "chat")
TASK_SUBTYPES = {"call": "Call", "email": "Email", "chat": "Task"}
DIRECTIONS = ("inbound", "outbound")
MAX_VALUE = 500
MAX_SUMMARY = 2000
MAX_FIELDS = 30
MAX_RECORDS = 10
SUBJECT_LIMIT = 255
_INSTANCE = re.compile(r"^https://([a-z0-9][a-z0-9-]{0,60})(\.sandbox|\.develop|\.scratch)?\.my\.salesforce\.com$")
_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}(\.sandbox|\.develop|\.scratch)?\.my\.salesforce\.com$")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,60}$")
_ID = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")
_REF = re.compile(r"^[A-Za-z0-9:._/#@-]{1,256}$")
_SHORT = re.compile(r"^[A-Za-z0-9 ._:/-]{1,100}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,59}$")
_SOSL_RESERVED = re.compile(r"""([?&|!{}\[\]()^~*:\\"'+\-])""")
Transport = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]


class SalesforceError(RuntimeError):
    """A Salesforce failure; ``code`` is one of ``rate-limited``, ``outage``, ``unauthorized`` or ``rejected``."""

    def __init__(self, message: str, code: str, status: int) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _module(name: str, relative: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parent / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _redact():
    return _module("model_garden_salesforce_contacts", "../core/contacts.py").redact_cards


def _default_transport() -> Transport:
    return _module("model_garden_salesforce_http", "http_common.py").make_transport("Salesforce", _HOST)


def sosl_term(text: str) -> str:
    """Escape a search term for SOSL ``FIND {...}``: every reserved character is backslash-escaped."""
    return _SOSL_RESERVED.sub(r"\\\1", text)


def _fields(config: Mapping[str, Sequence[str]], label: str) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for object_type, names in config.items():
        if object_type not in OBJECT_NAMES:
            raise ValueError(f"{label}: object type must be one of {', '.join(OBJECT_NAMES)}")
        listed = list(names)
        if not listed or len(listed) > 100 or not all(isinstance(name, str) and _FIELD.match(name) for name in listed):
            raise ValueError(f"{label}.{object_type}: list 1 to 100 Salesforce field names")
        if "Id" in listed:
            raise ValueError(f"{label}.{object_type}: Id is always returned as the object id; do not list it")
        result[object_type] = frozenset(listed)
    return result


class SalesforceClient:
    def __init__(
        self,
        access_token: str,
        *,
        instance_url: str,
        allowed_fields: Mapping[str, Sequence[str]],
        writable_fields: Mapping[str, Sequence[str]] | None = None,
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Salesforce access token must be supplied at runtime")
        if not isinstance(instance_url, str) or not _INSTANCE.match(instance_url):
            raise ValueError("instance_url must be the org's https://<name>.my.salesforce.com address")
        self._allowed = _fields(allowed_fields, "allowed_fields")
        if not self._allowed:
            raise ValueError("allowed_fields must list the fields each object type may expose; there is no allow-all")
        self._writable = _fields(writable_fields or {}, "writable_fields")
        for object_type, names in self._writable.items():
            if object_type not in self._allowed or not names <= self._allowed[object_type]:
                raise ValueError(f"writable_fields.{object_type} must be a subset of allowed_fields.{object_type}")
        self._token = access_token
        self._base = f"{instance_url}/services/data/{API_VERSION}"
        self._transport = transport or _default_transport()
        self._redact = _redact()

    # -- plumbing -----------------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, no_duplicates: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if no_duplicates:
            headers["Sforce-Duplicate-Rule-Header"] = "allowSave=false"
        status, body = self._transport(method, f"{self._base}{path}", headers, payload)
        if 200 <= status < 300:
            return body if body is not None else {}
        error = ""
        if isinstance(body, list) and body and isinstance(body[0], Mapping):
            error = str(body[0].get("errorCode", ""))
        elif isinstance(body, Mapping):
            error = str(body.get("errorCode", ""))
        error = error if _ERROR_CODE.match(error) else ""
        if error == "DUPLICATES_DETECTED":
            raise SalesforceError("Salesforce refused: a matching record already exists; records are never merged", "rejected", status)
        if status == 429 or error == "REQUEST_LIMIT_EXCEEDED":
            code = "rate-limited"
        elif status in (401, 403):
            code = "unauthorized"
        elif status >= 500:
            code = "outage"
        else:
            code = "rejected"
        raise SalesforceError(f"Salesforce request failed ({status}): {code}" + (f" [{error}]" if error else ""), code, status)

    def _object(self, arguments: Mapping[str, Any]) -> str:
        object_type = arguments.get("objectType", "contact")
        if object_type not in OBJECT_NAMES or object_type not in self._allowed:
            raise ValueError(f"objectType must be one of the configured types: {', '.join(sorted(self._allowed))}")
        return object_type

    @staticmethod
    def _object_id(value: Any) -> str:
        if not isinstance(value, str) or not _ID.match(value):
            raise ValueError("objectId must be a Salesforce record id (15 or 18 characters)")
        return value

    def _shape(self, object_type: str, record: Any) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise SalesforceError("Salesforce returned an invalid record", "outage", 200)
        object_id = str(record.get("Id") or record.get("id") or "")
        if not _ID.match(object_id):
            raise SalesforceError("Salesforce returned a record without a valid id", "outage", 200)
        allowed = self._allowed[object_type]
        fields = {name: str(value)[:MAX_VALUE] for name, value in record.items() if name in allowed and value is not None}
        return {"objectType": object_type, "objectId": object_id, "fields": fields, "externalObjectRef": f"salesforce:{object_type}/{object_id}"}

    def _writes(self, object_type: str, fields: Any) -> dict[str, str]:
        if not isinstance(fields, dict) or not fields or len(fields) > MAX_FIELDS:
            raise ValueError(f"fields must be an object of 1 to {MAX_FIELDS} fields")
        writable = self._writable.get(object_type, frozenset())
        refused = sorted(str(name) for name in fields if name not in writable)
        if refused:
            raise ValueError(f"these fields may not be written on {object_type}: {', '.join(refused)}")
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
        sobject = OBJECT_NAMES[object_type]
        names = sorted(self._allowed[object_type])
        object_id, query = arguments.get("objectId"), arguments.get("query")
        if (object_id is None) == (query is None):
            raise ValueError("crm.read needs exactly one of objectId or query")
        if object_id is not None:
            record = self._request("GET", f"/sobjects/{sobject}/{self._object_id(object_id)}?fields={','.join(names)}")
            return {"provider": "salesforce", "records": [self._shape(object_type, record)]}
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("query must be a non-empty string of at most 200 characters")
        sosl = f"FIND {{{sosl_term(query.strip())}}} IN ALL FIELDS RETURNING {sobject}(Id,{','.join(names)}) LIMIT {MAX_RECORDS}"
        found = self._request("GET", "/search/?q=" + urllib.parse.quote(sosl, safe=""))
        rows = found.get("searchRecords") if isinstance(found, Mapping) else None
        return {"provider": "salesforce", "records": [self._shape(object_type, item) for item in (rows or [])[:MAX_RECORDS]]}

    def update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("crm.update arguments must be a JSON object")
        object_type = self._object(arguments)
        object_id = self._object_id(arguments.get("objectId"))
        values = self._writes(object_type, arguments.get("fields"))
        self._request("PATCH", f"/sobjects/{OBJECT_NAMES[object_type]}/{object_id}", values)
        return {"provider": "salesforce", "objectId": object_id, "externalObjectRef": f"salesforce:{object_type}/{object_id}", "updated": sorted(values)}

    def create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Create a record; a duplicate is refused by Salesforce's rules, never merged or updated."""
        if not isinstance(arguments, dict):
            raise ValueError("crm.create arguments must be a JSON object")
        object_type = self._object(arguments)
        values = self._writes(object_type, arguments.get("fields"))
        created = self._request("POST", f"/sobjects/{OBJECT_NAMES[object_type]}", values, no_duplicates=True)
        object_id = self._object_id(str(created.get("id", "")) if isinstance(created, Mapping) else "")
        return {"provider": "salesforce", "objectId": object_id, "externalObjectRef": f"salesforce:{object_type}/{object_id}"}

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
        moment = moment.astimezone(dt.timezone.utc)
        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY:
            raise ValueError(f"summary must be a non-empty string of at most {MAX_SUMMARY} characters")
        outcome, reference = arguments.get("outcome"), arguments.get("transcriptRef")
        if outcome is not None and (not isinstance(outcome, str) or not _SHORT.match(outcome)):
            raise ValueError("outcome must be a short label")
        if reference is not None and (not isinstance(reference, str) or not _REF.match(reference)):
            raise ValueError("transcriptRef must be a reference to the stored transcript, never its text")
        cleaned = self._redact(summary.strip())
        material = json.dumps([object_type, object_id, moment.strftime("%Y-%m-%dT%H:%M:%SZ"), channel, direction, kind, cleaned], separators=(",", ":"))
        marker = "mgkey" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        # The marker is "mgkey" plus 32 hex digits we computed, so nothing caller-controlled reaches the SOQL.
        soql = f"SELECT Id FROM Task WHERE Subject LIKE '%{marker}%' LIMIT 1"  # nosec B608
        existing = self._request("GET", "/query?q=" + urllib.parse.quote(soql, safe=""))
        rows = existing.get("records") if isinstance(existing, Mapping) else None
        if rows and isinstance(rows[0], Mapping) and rows[0].get("Id"):
            task_id = self._object_id(str(rows[0]["Id"]))
            return {"provider": "salesforce", "interactionId": task_id, "externalObjectRef": f"salesforce:task/{task_id}", "duplicate": True}
        lead = f"{kind.title()} ({direction}): "
        subject = (lead + " ".join(cleaned.split()))[: SUBJECT_LIMIT - len(marker) - 1] + " " + marker
        lines = [f"[{kind} / {direction} via {channel}] {cleaned}"]
        if outcome:
            lines.append(f"Outcome: {outcome}")
        if reference:
            lines.append(f"Transcript reference: {reference}")
        task = {
            "Subject": subject, "Description": "\n".join(lines), "Status": "Completed", "ActivityDate": moment.strftime("%Y-%m-%d"),
            "TaskSubtype": TASK_SUBTYPES[kind], "WhoId" if object_type == "contact" else "WhatId": object_id,
        }
        created = self._request("POST", "/sobjects/Task", task)
        task_id = self._object_id(str(created.get("id", "")) if isinstance(created, Mapping) else "")
        return {"provider": "salesforce", "interactionId": task_id, "externalObjectRef": f"salesforce:task/{task_id}", "duplicate": False}

    def executors(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {"crm.read": self.read, "crm.update": self.update, "crm.create": self.create, "crm.log-interaction": self.log_interaction}
