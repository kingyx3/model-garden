"""Minimal Google Calendar connector for the Model Garden Receptionist MVP.

This module intentionally implements only the first proven business-system path:
read free/busy state and create a calendar event. OAuth/access-token acquisition,
secret storage, refresh, and broader Google Workspace administration remain outside
this connector. Callers inject credentials at runtime and route material writes
through Model Garden's governed action boundary.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]


def _parse_rfc3339(value: str, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _validated_window(start: str, end: str) -> tuple[dt.datetime, dt.datetime]:
    start_dt = _parse_rfc3339(start, "start")
    end_dt = _parse_rfc3339(end, "end")
    if end_dt <= start_dt:
        raise ValueError("end must be after start")
    return start_dt, end_dt


def _urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com":
        raise ValueError("Google Calendar transport only permits the fixed HTTPS Google API host")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # nosec B310 -- scheme and hostname are explicitly constrained immediately above.
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"error": {"message": raw or str(exc)}}
        return exc.code, body


class GoogleCalendarClient:
    """Small Google Calendar REST client with runtime-injected credentials."""

    def __init__(
        self,
        access_token: str,
        *,
        calendar_id: str = "primary",
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Google Calendar access token must be supplied at runtime")
        if not isinstance(calendar_id, str) or not calendar_id:
            raise ValueError("calendar_id must be non-empty")
        self._access_token = access_token
        self.calendar_id = calendar_id
        self._transport = transport or _urllib_transport

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        status, body = self._transport(method, url, self._headers, payload)
        if not 200 <= status < 300:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(f"Google Calendar API failed ({status}): {message or 'request failed'}")
        if not isinstance(body, dict):
            raise RuntimeError("Google Calendar API returned a non-object response")
        return body

    def check_availability(
        self,
        *,
        start: str,
        end: str,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Return whether the requested interval is free on the configured calendar."""
        _validated_window(start, end)
        payload: dict[str, Any] = {
            "timeMin": start,
            "timeMax": end,
            "items": [{"id": self.calendar_id}],
        }
        if timezone:
            payload["timeZone"] = timezone
        response = self._request("POST", f"{GOOGLE_CALENDAR_API}/freeBusy", payload)
        calendar = response.get("calendars", {}).get(self.calendar_id)
        if not isinstance(calendar, dict):
            raise RuntimeError("Google Calendar freeBusy response did not include the configured calendar")
        if calendar.get("errors"):
            raise RuntimeError("Google Calendar freeBusy returned a calendar-level error")
        busy = calendar.get("busy", [])
        if not isinstance(busy, list):
            raise RuntimeError("Google Calendar freeBusy returned invalid busy data")
        return {"available": len(busy) == 0, "busy": busy, "start": start, "end": end}

    def book(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Create one calendar event from the exact governed-action arguments."""
        if not isinstance(arguments, dict):
            raise ValueError("calendar.book arguments must be a JSON object")
        start = arguments.get("start") or arguments.get("slot")
        end = arguments.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("calendar.book requires start (or slot) and end")
        _validated_window(start, end)

        summary = arguments.get("summary", "Appointment")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string")

        timezone = arguments.get("timezone")
        if timezone is not None and (not isinstance(timezone, str) or not timezone):
            raise ValueError("timezone must be a non-empty string when supplied")

        start_value: dict[str, Any] = {"dateTime": start}
        end_value: dict[str, Any] = {"dateTime": end}
        if timezone:
            start_value["timeZone"] = timezone
            end_value["timeZone"] = timezone

        event: dict[str, Any] = {
            "summary": summary.strip(),
            "start": start_value,
            "end": end_value,
        }
        description = arguments.get("description")
        if isinstance(description, str) and description:
            event["description"] = description
        attendees = arguments.get("attendees")
        if attendees is not None:
            if not isinstance(attendees, list) or not all(isinstance(email, str) and email for email in attendees):
                raise ValueError("attendees must be a list of non-empty email strings")
            event["attendees"] = [{"email": email} for email in attendees]

        calendar_id = urllib.parse.quote(self.calendar_id, safe="")
        response = self._request(
            "POST",
            f"{GOOGLE_CALENDAR_API}/calendars/{calendar_id}/events?sendUpdates=none",
            event,
        )
        event_id = response.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise RuntimeError("Google Calendar create-event response did not include an event id")
        return {
            "provider": "google-calendar",
            "calendarId": self.calendar_id,
            "eventId": event_id,
            "htmlLink": response.get("htmlLink"),
            "start": response.get("start", start_value),
            "end": response.get("end", end_value),
            "status": response.get("status", "confirmed"),
        }

    def booking_executor(self) -> Callable[[dict[str, Any]], dict[str, Any]]:
        """Return the executor shape expected by scripts/govern-action.py."""
        return self.book
