"""Microsoft 365 mail connector (Microsoft Graph) for the portable ``mail.*`` Tools, limited to one configured mailbox.

Implements ``mail.search``, ``mail.read``, ``mail.draft`` and ``mail.send`` against Graph v1.0 behind an injected
transport, with the same arguments and result shapes as the Gmail adapter so one Agent works with either. It is
**draft-first**: ``mail.send`` accepts only the id of an existing draft (it first confirms the message is still a
draft), and the governed boundary treats it as ``external-communication`` needing exact-action approval by default.
OAuth consent, token acquisition and refresh and tenant administration are outside this module: the caller injects an
access token at runtime. The connection is bound to one mailbox (``/users/{mailbox}``) and no argument can name another.

Graph does not return the id of the sent item, so ``mail.send`` reports the draft id as ``messageId`` and flags
``sentFromDraft``; correlating the sent item is a follow-up. Status: credential-free tests only (a fake transport); the
sidecar does not serve these Tools yet and nothing here has had a live Microsoft 365 proof.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

GRAPH_API = "https://graph.microsoft.com/v1.0"
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]
SCOPES = {
    "mail.search": ["Mail.Read"],
    "mail.read": ["Mail.Read"],
    "mail.draft": ["Mail.ReadWrite"],
    "mail.send": ["Mail.Send"],
}
_SUMMARY_FIELDS = "id,subject,from,receivedDateTime,bodyPreview"


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
        raise ValueError("Microsoft Graph redirect refused")


def _urllib_transport(method: str, url: str, headers: dict[str, str], payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com" or parsed.port is not None:
        raise ValueError("Microsoft mail transport only permits the fixed HTTPS Microsoft Graph host")
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


def _address(entry: Any) -> str:
    address = (entry or {}).get("emailAddress") if isinstance(entry, dict) else None
    return str(address.get("address", "")) if isinstance(address, dict) else ""


class MicrosoftMailClient:
    def __init__(self, access_token: str, *, mailbox: str, transport: Transport | None = None) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Microsoft Graph access token must be supplied at runtime")
        self._common = _common()
        self.mailbox = self._common.check_mailbox(mailbox)
        self._token = access_token
        self._transport = transport or _urllib_transport

    @property
    def _base(self) -> str:
        return f"{GRAPH_API}/users/{urllib.parse.quote(self.mailbox, safe='@')}"

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None, *, text_body: bool = False) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json"}
        if text_body:
            headers["Prefer"] = 'outlook.body-content-type="text"'
        status, body = self._transport(method, url, headers, payload)
        if not 200 <= status < 300:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(f"Microsoft Graph mail failed ({status}): {message or 'request failed'}")
        if not isinstance(body, dict):
            raise RuntimeError("Microsoft Graph returned a non-object response")
        return body

    def _ref(self, kind: str, identifier: str) -> str:
        return f"outlook:{kind}/{identifier}"

    # -- portable Tools -----------------------------------------------------------------

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query, limit = self._common.search_arguments(arguments)
        quoted = urllib.parse.quote('"' + query.replace('"', "") + '"', safe="")
        listing = self._request("GET", f"{self._base}/messages?$search={quoted}&$top={limit}&$select={_SUMMARY_FIELDS}")
        messages = []
        for item in (listing.get("value") or [])[:limit]:
            if not isinstance(item, dict):
                continue
            identifier = self._common.opaque_id(item.get("id"), "message id")
            messages.append({**self._common.summary(identifier, _address(item.get("from")), item.get("subject"),
                                                    item.get("receivedDateTime"), item.get("bodyPreview")),
                             "externalObjectRef": self._ref("message", identifier)})
        return {"provider": "outlook", "mailbox": self.mailbox, "messages": messages}

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        identifier = self._common.id_argument(arguments, "messageId")
        detail = self._request(
            "GET",
            f"{self._base}/messages/{urllib.parse.quote(identifier, safe='')}"
            "?$select=id,subject,from,toRecipients,receivedDateTime,bodyPreview,body",
            text_body=True,
        )
        body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
        content = body.get("content") if body.get("contentType") == "text" else ""
        message = {
            **self._common.summary(identifier, _address(detail.get("from")), detail.get("subject"), detail.get("receivedDateTime"),
                                   detail.get("bodyPreview")),
            "to": [address for address in (_address(item) for item in detail.get("toRecipients") or []) if address][:50],
            "body": (content if isinstance(content, str) else "")[: self._common.MAX_READ_BODY],
            "externalObjectRef": self._ref("message", identifier),
        }
        return {"provider": "outlook", "mailbox": self.mailbox, "message": message}

    def draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        recipients, subject, body = self._common.draft_arguments(arguments)
        created = self._request("POST", f"{self._base}/messages", {
            "subject": subject, "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": address}} for address in recipients],
        })
        draft_id = self._common.opaque_id(created.get("id"), "draft id")
        return {"provider": "outlook", "mailbox": self.mailbox, "draftId": draft_id, "externalObjectRef": self._ref("draft", draft_id)}

    def send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Send an existing draft by id; a message that is not a draft is refused."""
        draft_id = self._common.id_argument(arguments, "draftId")
        quoted = urllib.parse.quote(draft_id, safe="")
        state = self._request("GET", f"{self._base}/messages/{quoted}?$select=id,isDraft")
        if state.get("isDraft") is not True:
            raise ValueError("mail.send only sends a message that is still a draft")
        self._request("POST", f"{self._base}/messages/{quoted}/send")
        return {"provider": "outlook", "mailbox": self.mailbox, "messageId": draft_id, "draftId": draft_id, "sentFromDraft": True,
                "externalObjectRef": self._ref("message", draft_id)}

    def executors(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {"mail.search": self.search, "mail.read": self.read, "mail.draft": self.draft, "mail.send": self.send}
