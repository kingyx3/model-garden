"""Gmail connector for the portable ``mail.*`` Tools, limited to one configured mailbox.

Implements ``mail.search``, ``mail.read``, ``mail.draft`` and ``mail.send`` against the Gmail REST API behind an injected
transport. It is **draft-first**: a message exists only as a draft until ``mail.send`` is given that draft's id, and the
governed boundary treats ``mail.send`` as ``external-communication`` that needs exact-action approval by default.
OAuth consent, token acquisition and refresh, scopes granted, and secret storage are outside this module: the caller
injects an access token at runtime. The connection is bound to a single mailbox and no argument can name another, so
there is no tenant-wide mail access. Message and draft ids come back as ``externalObjectRef`` values so the ledger
records references, never bodies.

Status: credential-free tests only (a fake transport). The sidecar does not serve these Tools yet and nothing here has
had a live Gmail proof.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]
SCOPES = {
    "mail.search": ["https://www.googleapis.com/auth/gmail.readonly"],
    "mail.read": ["https://www.googleapis.com/auth/gmail.readonly"],
    "mail.draft": ["https://www.googleapis.com/auth/gmail.compose"],
    "mail.send": ["https://www.googleapis.com/auth/gmail.send"],
}


def _common():
    name = "model_garden_mail_common"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("mail_common.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Gmail redirect refused")


def _urllib_transport(method: str, url: str, headers: dict[str, str], payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "gmail.googleapis.com" or parsed.port is not None:
        raise ValueError("Gmail transport only permits the fixed HTTPS Gmail API host")
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
            return exc.code, {"error": {"message": raw or str(exc)}}


def _header(headers: Any, name: str) -> str:
    for item in headers if isinstance(headers, list) else []:
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
            return str(item.get("value", ""))
    return ""


def _text_body(payload: Any, limit: int) -> str:
    """The first text/plain part (depth-first), decoded; HTML-only mail yields an empty body rather than markup."""
    stack = [payload] if isinstance(payload, dict) else []
    while stack:
        part = stack.pop(0)
        if part.get("mimeType") == "text/plain":
            data = (part.get("body") or {}).get("data")
            if isinstance(data, str) and data:
                try:
                    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")[:limit]
                except (ValueError, TypeError):
                    return ""
        stack = [child for child in part.get("parts", []) if isinstance(child, dict)] + stack
    return ""


class GmailClient:
    def __init__(self, access_token: str, *, mailbox: str, transport: Transport | None = None) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Gmail access token must be supplied at runtime")
        self._common = _common()
        self.mailbox = self._common.check_mailbox(mailbox)
        self._token = access_token
        self._transport = transport or _urllib_transport

    @property
    def _base(self) -> str:
        return f"{GMAIL_API}/users/{urllib.parse.quote(self.mailbox, safe='')}"

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json"}
        status, body = self._transport(method, url, headers, payload)
        if not 200 <= status < 300:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(f"Gmail API failed ({status}): {message or 'request failed'}")
        if not isinstance(body, dict):
            raise RuntimeError("Gmail API returned a non-object response")
        return body

    def _ref(self, kind: str, identifier: str) -> str:
        return f"gmail:{kind}/{identifier}"

    # -- portable Tools -----------------------------------------------------------------

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query, limit = self._common.search_arguments(arguments)
        listing = self._request("GET", f"{self._base}/messages?q={urllib.parse.quote(query, safe='')}&maxResults={limit}")
        messages = []
        for item in (listing.get("messages") or [])[:limit]:
            identifier = self._common.opaque_id(item.get("id") if isinstance(item, dict) else None, "message id")
            detail = self._request(
                "GET",
                f"{self._base}/messages/{urllib.parse.quote(identifier, safe='')}?format=metadata"
                "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date",
            )
            headers = (detail.get("payload") or {}).get("headers")
            messages.append({**self._common.summary(identifier, _header(headers, "From"), _header(headers, "Subject"),
                                                    _header(headers, "Date"), detail.get("snippet")),
                             "externalObjectRef": self._ref("message", identifier)})
        return {"provider": "gmail", "mailbox": self.mailbox, "messages": messages}

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        identifier = self._common.id_argument(arguments, "messageId")
        detail = self._request("GET", f"{self._base}/messages/{urllib.parse.quote(identifier, safe='')}?format=full")
        headers = (detail.get("payload") or {}).get("headers")
        message = {
            **self._common.summary(identifier, _header(headers, "From"), _header(headers, "Subject"), _header(headers, "Date"), detail.get("snippet")),
            "to": [part.strip() for part in _header(headers, "To").split(",") if part.strip()][:50],
            "body": _text_body(detail.get("payload"), self._common.MAX_READ_BODY),
            "externalObjectRef": self._ref("message", identifier),
        }
        return {"provider": "gmail", "mailbox": self.mailbox, "message": message}

    def draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        recipients, subject, body = self._common.draft_arguments(arguments)
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.mailbox, ", ".join(recipients), subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        created = self._request("POST", f"{self._base}/drafts", {"message": {"raw": raw}})
        draft_id = self._common.opaque_id(created.get("id"), "draft id")
        return {"provider": "gmail", "mailbox": self.mailbox, "draftId": draft_id, "externalObjectRef": self._ref("draft", draft_id)}

    def send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Send an existing draft by id; there is no way to send a message that was not drafted first."""
        draft_id = self._common.id_argument(arguments, "draftId")
        sent = self._request("POST", f"{self._base}/drafts/send", {"id": draft_id})
        message_id = self._common.opaque_id(sent.get("id"), "sent message id")
        return {"provider": "gmail", "mailbox": self.mailbox, "messageId": message_id, "draftId": draft_id,
                "externalObjectRef": self._ref("message", message_id)}

    def executors(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        """Executor per portable Tool name, in the shape ``scripts/govern-action.py`` expects."""
        return {"mail.search": self.search, "mail.read": self.read, "mail.draft": self.draft, "mail.send": self.send}
