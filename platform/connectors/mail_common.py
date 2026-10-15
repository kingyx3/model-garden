"""Shared validation and result shapes for the mail connectors (Gmail, Microsoft Graph).

The portable ``mail.*`` Tools (``contracts/v1/capabilities.json``) take the same arguments and return the same shapes
whichever provider is bound, so one Agent and one Skill work with either. This module holds only what both adapters
must do identically: bound the inputs, refuse header injection, refuse any attempt to name a different mailbox, and
shape results. It has no network code and no credentials.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MAX_RECIPIENTS = 20
MAX_SUBJECT = 255
MAX_BODY = 50_000
MAX_QUERY = 200
MAX_LIMIT = 25
DEFAULT_LIMIT = 10
MAX_READ_BODY = 20_000
MAX_SNIPPET = 300
MAILBOX = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
ADDRESS = re.compile(r"^[^@\s<>,;\"\\]+@[^@\s<>,;\"\\]+\.[^@\s<>,;\"\\]+$")
OPAQUE_ID = re.compile(r"^[A-Za-z0-9_=+-]{1,512}$")  # provider ids are URL-safe base64 or hex; no "/", "." or ":"
_MAILBOX_KEYS = ("mailbox", "userId", "user", "account", "from", "sender")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def check_mailbox(value: Any) -> str:
    if not isinstance(value, str) or not MAILBOX.match(value):
        raise ValueError("mailbox must be the one configured mailbox address; there is no tenant-wide mail access")
    return value


def refuse_mailbox_arguments(arguments: Mapping[str, Any]) -> None:
    """A model or caller can never choose a different mailbox or sender: the connection fixes both."""
    if not isinstance(arguments, Mapping):
        raise ValueError("mail arguments must be a JSON object")
    named = [key for key in _MAILBOX_KEYS if key in arguments]
    if named:
        raise ValueError(f"{', '.join(named)} cannot be set by a call; the connection is limited to its configured mailbox")


def opaque_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID.match(value):
        raise ValueError(f"{label} must be an identifier returned by the provider")
    return value


def search_arguments(arguments: Mapping[str, Any]) -> tuple[str, int]:
    refuse_mailbox_arguments(arguments)
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY or _CONTROL.search(query):
        raise ValueError(f"query must be a non-empty string of at most {MAX_QUERY} characters")
    limit = arguments.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return query.strip(), limit


def draft_arguments(arguments: Mapping[str, Any]) -> tuple[list[str], str, str]:
    refuse_mailbox_arguments(arguments)
    extra = sorted(set(arguments) - {"to", "subject", "body"})
    if extra:
        raise ValueError("mail.draft supports only to, subject and body: " + ", ".join(extra))
    recipients = arguments.get("to")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= MAX_RECIPIENTS or not all(
        isinstance(item, str) and ADDRESS.match(item) and len(item) <= 254 for item in recipients
    ):
        raise ValueError(f"to must be 1 to {MAX_RECIPIENTS} plain email addresses")
    subject, body = arguments.get("subject"), arguments.get("body")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > MAX_SUBJECT or "\r" in subject or "\n" in subject or _CONTROL.search(subject):
        raise ValueError(f"subject must be one line of at most {MAX_SUBJECT} characters")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
        raise ValueError(f"body must be a non-empty string of at most {MAX_BODY} characters")
    return list(dict.fromkeys(recipients)), subject.strip(), _CONTROL.sub("", body)


def id_argument(arguments: Mapping[str, Any], key: str) -> str:
    refuse_mailbox_arguments(arguments)
    extra = sorted(set(arguments) - {key})
    if extra:
        raise ValueError(f"only {key} is accepted: " + ", ".join(extra))
    return opaque_id(arguments.get(key), key)


def summary(message_id: str, sender: Any, subject: Any, date: Any, snippet: Any) -> dict[str, Any]:
    return {
        "id": message_id,
        "from": sender if isinstance(sender, str) else "",
        "subject": subject if isinstance(subject, str) else "",
        "date": date if isinstance(date, str) else "",
        "snippet": (snippet if isinstance(snippet, str) else "")[:MAX_SNIPPET],
    }
