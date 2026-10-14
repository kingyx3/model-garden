"""Bounded Microsoft 365 Calendar adapter behind the Model Garden governed Tool contract.

Supports only Graph v1.0 free/busy and event creation for an explicitly configured
mailbox. OAuth consent, token acquisition/refresh, and Microsoft tenant management
are operator/client authorization concerns, not Agent or Skill capabilities.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

GRAPH_API = "https://graph.microsoft.com/v1.0"
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]
MAILBOX = re.compile(r"^[^@\s/]+@[^@\s/]+$")


def _window(start: str, end: str) -> tuple[dt.datetime, dt.datetime]:
    def parse(value: str, name: str) -> dt.datetime:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty RFC3339 timestamp")
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an RFC3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{name} must include a timezone offset")
        return parsed.astimezone(dt.timezone.utc)

    first, last = parse(start, "start"), parse(end, "end")
    if last <= first:
        raise ValueError("end must be after start")
    return first, last


def _utc_object(timestamp: dt.datetime) -> dict[str, str]:
    return {"dateTime": timestamp.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Microsoft Graph redirect refused")


def _transport(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com" or parsed.port is not None:
        raise ValueError("Microsoft Calendar only permits the fixed HTTPS Microsoft Graph host")
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        # nosec B310 -- URL host/scheme are fixed above; redirects are denied.
        with opener.open(request, timeout=15) as response:
            data = response.read().decode("utf-8")
            return response.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        # Do not return/echo Graph's raw error body: it can include private mailbox details.
        return exc.code, {}


class MicrosoftCalendarClient:
    """Provider adapter preserving calendar.availability/calendar.book's generic result shape."""

    def __init__(self, access_token: str, *, mailbox: str, transport: Transport | None = None) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Microsoft Calendar access token must be supplied at runtime")
        if not isinstance(mailbox, str) or not MAILBOX.fullmatch(mailbox):
            raise ValueError("Microsoft Calendar requires an approved mailbox email address")
        self._access_token = access_token
        self.mailbox = mailbox
        self._transport = transport or _transport
        self._prefix = f"{GRAPH_API}/users/{urllib.parse.quote(mailbox, safe='')}"

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._transport(
            "POST", self._prefix + path,
            {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json",
             "Accept": "application/json"}, payload,
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"Microsoft Graph Calendar API failed ({status})")
        if not isinstance(body, dict):
            raise RuntimeError("Microsoft Graph Calendar returned a non-object response")
        return body

    def check_availability(self, *, start: str, end: str, timezone: str | None = None) -> dict[str, Any]:
        first, last = _window(start, end)
        if timezone is not None and (not isinstance(timezone, str) or not timezone.strip()):
            raise ValueError("timezone must be a non-empty string when supplied")
        response = self._request("/calendar/getSchedule", {
            "schedules": [self.mailbox],
            "startTime": _utc_object(first), "endTime": _utc_object(last),
            "availabilityViewInterval": 30,
        })
        values = response.get("value")
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise RuntimeError("Microsoft Graph Calendar schedule response is incomplete")
        schedule = values[0]
        if schedule.get("scheduleId", "").casefold() != self.mailbox.casefold() or schedule.get("error"):
            raise RuntimeError("Microsoft Graph Calendar schedule unavailable for the approved mailbox")
        items = schedule.get("scheduleItems")
        if not isinstance(items, list) or any(not isinstance(item, dict) or "status" not in item for item in items):
            raise RuntimeError("Microsoft Graph Calendar schedule items are invalid")
        # Be conservative: tentative, busy, OOF and unknown statuses block booking.
        busy = [item for item in items if item["status"] != "free"]
        return {"available": not busy, "busy": busy, "start": start, "end": end}

    def book(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("calendar.book arguments must be a JSON object")
        start = arguments.get("start") or arguments.get("slot")
        end = arguments.get("end")
        first, last = _window(start, end)
        title = arguments.get("summary", "Appointment")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("summary must be a non-empty string")
        timezone = arguments.get("timezone")
        if timezone is not None and (not isinstance(timezone, str) or not timezone.strip()):
            raise ValueError("timezone must be a non-empty string when supplied")
        payload: dict[str, Any] = {
            "subject": title.strip(),
            "start": _utc_object(first),
            "end": _utc_object(last),
        }
        description = arguments.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise ValueError("description must be a string")
            if description:
                payload["body"] = {"contentType": "text", "content": description}
        attendees = arguments.get("attendees")
        if attendees is not None:
            if not isinstance(attendees, list) or not all(isinstance(email, str) and MAILBOX.fullmatch(email) for email in attendees):
                raise ValueError("attendees must be a list of email addresses")
            # Microsoft Graph sends invitations when an event has attendees.
            payload["attendees"] = [{"emailAddress": {"address": email}, "type": "required"} for email in attendees]
        response = self._request("/events", payload)
        event_id = response.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise RuntimeError("Microsoft Graph Calendar create-event response has no event id")
        return {
            "provider": "microsoft-graph-calendar", "calendarId": self.mailbox,
            "eventId": event_id, "htmlLink": response.get("webLink"),
            "start": response.get("start", payload["start"]),
            "end": response.get("end", payload["end"]),
            "status": "confirmed",
        }

    def booking_executor(self) -> Callable[[dict[str, Any]], dict[str, Any]]:
        return self.book
