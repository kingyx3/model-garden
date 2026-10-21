"""Consent, opt-out and idempotency for customer booking notices (#240).

A confirmation or a reminder is a message to a customer, so before any sender exists it needs one place that answers "may we, and have we
already?". This is that place; it sends nothing and has no provider. A future booking-notice sender must call ``decide`` and ``claim``.

- **Consent by verified recipient and channel.** A preference is kept per ``(channel, recipient)``; the recipient is stored only as a
  SHA-256 digest of its normalised form, never the address. ``granted`` needs a ``verified_via`` (the recipient replied, or answered a code,
  or a named person confirmed it): a CRM match alone is not evidence that the customer controls a destination. ``opted-out`` is accepted
  on any evidence, always wins, and stays until the recipient grants again.
- **Purposes.** ``confirmation`` (a transactional message about a booking the customer made) and ``reminder`` (needs its own explicit
  opt-in). Nothing here is marketing; there is no purpose for it.
- **Decision.** ``decide`` returns ``send`` or a reason it is held back: ``no-consent``, ``opted-out`` or ``quiet-hours`` (a reminder is
  not sent between 21:00 and 08:00 in the recipient's own UTC offset). A confirmation ignores quiet hours because the customer just acted.
- **One claim per booking state.** ``claim`` is true once for a booking, a state (``confirmed``, ``rescheduled``, ``cancelled``), a channel
  and a recipient, so a retried or duplicated event sends nothing twice and a reschedule is a new state, not a repeat.
- **Bounded, deletable, minimal.** One JSON document per tenant (0600, 0700 directory, atomic, locked), at most ``MAX_RECIPIENTS`` recipients
  and ``MAX_CLAIMS`` claims (oldest first out). ``forget`` removes a recipient's preferences (claims are bare hashes and times that cannot be matched back to a person and age out). The ledger gets digests, purposes,
  states and reasons, never an address or a booking detail.

No live-deployment proof, and no sender: there is no booking event, provider or delivery wiring behind this yet.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Iterator
from typing import Any

SCHEMA_VERSION = 1
CHANNELS = ("email", "whatsapp", "sms", "chat")
PURPOSES = ("confirmation", "reminder")
STATES = ("granted", "opted-out")
VERIFIED_VIA = ("recipient-reply", "recipient-code", "named-person")
BOOKING_STATES = ("confirmed", "rescheduled", "cancelled")
QUIET_FROM, QUIET_TO = 21, 8
MAX_RECIPIENTS = 5000
MAX_CLAIMS = 20000
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_BOOKING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
Audit = Callable[[str, str, dict[str, Any]], Any]


class NotificationError(ValueError):
    """The request is malformed or not allowed; the message is safe to show an operator."""


def recipient_digest(channel: str, address: str) -> str:
    """The only form in which a recipient is kept: a digest of the channel and the normalised address or number."""
    if channel not in CHANNELS:
        raise NotificationError("channel is one of " + ", ".join(CHANNELS))
    if not isinstance(address, str) or not address.strip():
        raise NotificationError("a recipient address is required")
    text = address.strip().lower()
    if channel in ("whatsapp", "sms"):
        digits = re.sub(r"\D", "", text)
        if len(digits) < 6:
            raise NotificationError("a phone number has at least 6 digits")
        text = digits.lstrip("0")
    elif channel == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise NotificationError("an email recipient is an address")
    return hashlib.sha256(f"{channel}\0{text}".encode("utf-8")).hexdigest()[:24]


def local_hour(now: int, utc_offset_minutes: int) -> int:
    return dt.datetime.fromtimestamp(int(now) + int(utc_offset_minutes) * 60, dt.timezone.utc).hour


def render_booking_notice(state: str, *, start: int, utc_offset_minutes: int, contact: str, label: str = "your booking") -> str:
    """A short transactional text with the booking time in the recipient's own offset; the booking's other details are not included."""
    if state not in BOOKING_STATES:
        raise NotificationError("a booking state is " + ", ".join(BOOKING_STATES))
    if not -720 <= int(utc_offset_minutes) <= 840:
        raise NotificationError("utc_offset_minutes is between -720 and 840")
    moment = dt.datetime.fromtimestamp(int(start) + int(utc_offset_minutes) * 60, dt.timezone.utc)
    sign = "+" if utc_offset_minutes >= 0 else "-"
    zone = f"UTC{sign}{abs(utc_offset_minutes) // 60:02d}:{abs(utc_offset_minutes) % 60:02d}"
    when = f"{moment:%A %d %B %Y at %H:%M} ({zone})"
    verb = {"confirmed": "is confirmed for", "rescheduled": "has moved to", "cancelled": "was cancelled; it had been set for"}[state]
    return f"{label[:60].capitalize()} {verb} {when}. To change or cancel it, contact {contact[:120]}. Reply STOP to stop these messages."


class NotificationPrefs:
    def __init__(self, root: str | os.PathLike[str], *, tenant: str, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise NotificationError("a tenant is a short lowercase slug")
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.notification-prefs.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "recipients": {}, "claims": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise NotificationError("the notification store has an unsupported format")
                state = loaded
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                temporary = self.path.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(state, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _note(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def record(self, channel: str, address: str, purpose: str, state: str, *, by: str, now: int, verified_via: str | None = None) -> dict[str, Any]:
        """Record a recipient's choice. ``granted`` needs how the recipient was verified; ``opted-out`` needs nothing and always wins."""
        if purpose not in PURPOSES:
            raise NotificationError("purpose is one of " + ", ".join(PURPOSES))
        if state not in STATES:
            raise NotificationError("state is granted or opted-out")
        if not isinstance(by, str) or not _PERSON.match(by.strip()):
            raise NotificationError("who is recording this is required")
        if state == "granted" and verified_via not in VERIFIED_VIA:
            raise NotificationError("consent needs evidence that the recipient controls the destination: " + ", ".join(VERIFIED_VIA))
        key = recipient_digest(channel, address)
        with self._locked() as store:
            entry = store["recipients"].get(key)
            if entry is None:
                if len(store["recipients"]) >= MAX_RECIPIENTS:
                    raise NotificationError("the notification store is full; a person must forget recipients first")
                entry = {"channel": channel, "digest": key, "consents": {}, "createdAt": int(now)}
                store["recipients"][key] = entry
            entry["consents"][purpose] = {"state": state, "at": int(now), "by": by.strip()[:128], **({"verifiedVia": verified_via} if state == "granted" else {})}
            entry["updatedAt"] = int(now)
            result = json.loads(json.dumps(entry))
        self._note("notification-prefs.recorded", by.strip(), {"recipient": key, "channel": channel, "purpose": purpose, "state": state,
                                                              **({"verifiedVia": verified_via} if state == "granted" else {})})
        return result

    def decide(self, channel: str, address: str, purpose: str, *, now: int, utc_offset_minutes: int = 0) -> dict[str, Any]:
        """``{"send": True}`` or ``{"send": False, "reason": ...}``; never raises for a recipient it does not know (that is ``no-consent``)."""
        if purpose not in PURPOSES:
            raise NotificationError("purpose is one of " + ", ".join(PURPOSES))
        key = recipient_digest(channel, address)
        with self._locked() as store:
            entry = store["recipients"].get(key)
        consents = (entry or {}).get("consents", {})
        if any(item["state"] == "opted-out" for item in consents.values()):
            return {"send": False, "reason": "opted-out"}  # a stop on any purpose stops every notice to that destination
        if consents.get(purpose, {}).get("state") != "granted":
            return {"send": False, "reason": "no-consent"}
        if purpose == "reminder":
            hour = local_hour(now, utc_offset_minutes)
            if hour >= QUIET_FROM or hour < QUIET_TO:
                return {"send": False, "reason": "quiet-hours"}
        return {"send": True}

    def claim(self, booking_id: str, booking_state: str, channel: str, address: str, purpose: str, *, now: int) -> bool:
        """True the first time a notice for this booking, state, channel and recipient is claimed, so a duplicate event sends nothing twice."""
        if not isinstance(booking_id, str) or not _BOOKING.match(booking_id):
            raise NotificationError("a booking id is a short identifier")
        if booking_state not in BOOKING_STATES or purpose not in PURPOSES:
            raise NotificationError("unknown booking state or purpose")
        key = hashlib.sha256(f"{booking_id}\0{booking_state}\0{purpose}\0{recipient_digest(channel, address)}".encode("utf-8")).hexdigest()[:32]
        with self._locked() as store:
            if key in store["claims"]:
                return False
            store["claims"][key] = int(now)
            if len(store["claims"]) > MAX_CLAIMS:
                for old in sorted(store["claims"], key=store["claims"].get)[: len(store["claims"]) - MAX_CLAIMS]:
                    del store["claims"][old]
        return True

    def suppressed(self, reason: str, *, channel: str, address: str, purpose: str, booking_id: str, by: str = "runtime:notices") -> None:
        """Ledger that a notice was held back and why (digest, purpose and reason only)."""
        self._note("notification-prefs.suppressed", by, {"recipient": recipient_digest(channel, address), "channel": channel, "purpose": purpose,
                                                       "reason": reason, "booking": booking_id[:64]})

    def status(self, channel: str, address: str) -> dict[str, Any]:
        key = recipient_digest(channel, address)
        with self._locked() as store:
            entry = store["recipients"].get(key)
        return {"recipient": key, "channel": channel, "consents": json.loads(json.dumps((entry or {}).get("consents", {})))}

    def forget(self, channel: str, address: str, *, by: str) -> dict[str, int]:
        """Delete a recipient's preferences and claims (a deletion request); an opt-out is deleted with the rest, so keep it unless asked."""
        if not isinstance(by, str) or not _PERSON.match(by.strip()):
            raise NotificationError("who is deleting this is required")
        key = recipient_digest(channel, address)
        with self._locked() as store:
            # Claims are keyed by a hash that includes the recipient digest, so they cannot be matched back to a person: they are bare hashes and times.
            removed = 1 if store["recipients"].pop(key, None) is not None else 0
        self._note("notification-prefs.forgotten", by.strip(), {"recipient": key, "channel": channel, "removed": removed})
        return {"recipients": removed}

    def counts(self) -> dict[str, int]:
        with self._locked() as store:
            return {"recipients": len(store["recipients"]), "claims": len(store["claims"])}
