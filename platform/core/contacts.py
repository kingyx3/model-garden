"""Tier 0 built-in contact and interaction log: a customer record with no CRM connected.

A client with no CRM still needs every call and email to land somewhere a person can follow up.
This store backs `crm.create`, `crm.log-interaction` and `crm.read` on the governed sidecar. It is
deliberately conservative about identity and privacy:

- **No merging on an assertion.** `create_contact` always makes a new record. A name, phone number
  or email address stated by a caller or typed in an email is an assertion, not verified identity,
  so it is stored as `asserted`. If it collides with an existing record the pair goes to a human
  review queue; the response never says so, because that would confirm to an attacker that the
  number belongs to a customer.
- **No guessing a contact.** An interaction is logged against a contact ID the caller already holds,
  or against the `unmatched` queue for a person to assign. An unknown ID is an error.
- **Idempotent filing.** An interaction's ID is derived from its content, so a retry, a redelivered
  event or a restarted worker returns the same ID with `duplicate: true` and writes nothing.
- **No read-back to unverified principals.** `read` returns personal data only when the governed
  boundary says the principal is a verified `member`; anything else (including an unmapped static
  runtime identity, as on the voice channel today) gets no records.
- **Bounded, private, erasable.** Field sizes and record counts are capped, card numbers in
  summaries are redacted, the store is mode 0600 in a 0700 directory, and a contact (with its
  interactions) can be erased.

The store is one JSON document rewritten atomically under an exclusive file lock; it is sized for a
small business (thousands of records), not for a CRM replacement. Records are plaintext at rest on
the sidecar-only volume, like the ledger and approval store. Nothing here has had live-deployment
proof.
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
import secrets
import time
from collections.abc import Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
MAX_CONTACTS = 5000
MAX_INTERACTIONS = 20000
MAX_REVIEW = 1000
MAX_FIELD = 200
MAX_NOTES = 500
MAX_SUMMARY = 2000
MAX_OUTCOME = 100
MAX_QUERY = 100
MAX_READ = 10
MAX_INTERACTIONS_PER_READ = 10
INTERACTION_TYPES = ("call", "email", "chat")
DIRECTIONS = ("inbound", "outbound")
CONTACT_FIELDS = ("name", "phone", "email", "notes")
UNMATCHED = "unmatched"
PROVIDER_ID = "built-in-crm"
REDACTED_CARD = "[REDACTED:card]"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REF = re.compile(r"^[A-Za-z0-9:._/#@-]{1,256}$")
_CARD_CANDIDATE = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")


class ContactError(ValueError):
    """Raised for invalid input or an unusable record; never carries record contents."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact_cards(text: str) -> str:
    """Replace Luhn-valid 13-19 digit payment card numbers (same rule as the call transcript)."""

    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return REDACTED_CARD if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)

    return _CARD_CANDIDATE.sub(replace, text)


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    return digits if 7 <= len(digits) <= 15 else None


def normalize_email(value: str) -> str | None:
    cleaned = value.strip().lower()
    return cleaned if len(cleaned) <= 254 and _EMAIL.match(cleaned) else None


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContactError(f"{label} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise ContactError(f"{label} exceeds {limit} characters")
    return cleaned


def _timestamp(value: Any) -> str:
    text = _text(value, "timestamp", 64)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContactError("timestamp must be RFC3339 with a UTC offset") from exc
    if parsed.tzinfo is None:
        raise ContactError("timestamp must be RFC3339 with a UTC offset")
    return text


def _empty() -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "contacts": {}, "interactions": {}, "review": []}


class ContactStore:
    """File-backed store under ``root``; every public method is atomic under an exclusive lock."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / "contacts.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state = _empty()
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise ContactError("contact store has an unsupported format")
                state = loaded
            before = _canonical(state)
            yield state
            if _canonical(state) != before:
                temporary = self.path.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(_canonical(state) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    # -- writes ---------------------------------------------------------------------------

    def create_contact(self, fields: Mapping[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """Create a new contact from asserted details. Never merges with an existing record."""
        if not isinstance(fields, Mapping) or not fields:
            raise ContactError("fields must be an object with at least a name, phone or email")
        unknown = sorted(set(fields) - set(CONTACT_FIELDS))
        if unknown:
            raise ContactError(f"unsupported contact field(s): {', '.join(unknown)}")
        stored: dict[str, str] = {}
        keys: dict[str, str] = {}
        if "name" in fields:
            stored["name"] = _text(fields["name"], "name", MAX_FIELD)
        if "phone" in fields:
            raw = _text(fields["phone"], "phone", MAX_FIELD)
            normalized = normalize_phone(raw)
            if normalized is None:
                raise ContactError("phone must contain 7 to 15 digits")
            stored["phone"], keys["phone"] = raw, normalized
        if "email" in fields:
            raw = _text(fields["email"], "email", MAX_FIELD)
            normalized = normalize_email(raw)
            if normalized is None:
                raise ContactError("email is not a valid address")
            stored["email"], keys["email"] = raw, normalized
        if "notes" in fields:
            stored["notes"] = redact_cards(_text(fields["notes"], "notes", MAX_NOTES))
        if not (set(stored) & {"name", "phone", "email"}):
            raise ContactError("a contact needs at least a name, phone or email")
        created = int(time.time()) if now is None else now
        with self._locked() as state:
            if len(state["contacts"]) >= MAX_CONTACTS:
                raise ContactError("the contact log is full; a person must archive or erase contacts")
            contact_id = "c-" + secrets.token_hex(8)
            state["nextSeq"] = state.get("nextSeq", 0) + 1  # stable order even within one second
            collisions = sorted(
                other_id for other_id, other in state["contacts"].items()
                if any(other.get("keys", {}).get(kind) == value for kind, value in keys.items())
            )
            state["contacts"][contact_id] = {
                "id": contact_id, "fields": stored, "keys": keys,
                "identityEvidence": "asserted", "createdAt": created, "seq": state["nextSeq"],
            }
            if collisions and len(state["review"]) < MAX_REVIEW:
                state["review"].append({
                    "id": "r-" + secrets.token_hex(6), "kind": "possible-duplicate",
                    "contacts": [contact_id, *collisions[:5]], "createdAt": created,
                })
        return {"objectId": contact_id, "created": True, "externalObjectRef": f"{PROVIDER_ID}:contact/{contact_id}"}

    def log_interaction(self, arguments: Mapping[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """File one interaction against an existing contact or the `unmatched` queue; idempotent."""
        contact_id = _text(arguments.get("objectId"), "objectId", 64)
        kind = arguments.get("interactionType")
        direction = arguments.get("direction")
        if kind not in INTERACTION_TYPES:
            raise ContactError(f"interactionType must be one of {', '.join(INTERACTION_TYPES)}")
        if direction not in DIRECTIONS:
            raise ContactError(f"direction must be one of {', '.join(DIRECTIONS)}")
        channel = _text(arguments.get("channel"), "channel", MAX_FIELD)
        timestamp = _timestamp(arguments.get("timestamp"))
        summary = redact_cards(_text(arguments.get("summary"), "summary", MAX_SUMMARY))
        record: dict[str, Any] = {
            "type": kind, "channel": channel, "direction": direction, "timestamp": timestamp, "summary": summary,
        }
        if arguments.get("outcome") is not None:
            record["outcome"] = _text(arguments["outcome"], "outcome", MAX_OUTCOME)
        reference = arguments.get("transcriptRef")
        if reference is not None:
            if not isinstance(reference, str) or not _REF.match(reference):
                raise ContactError("transcriptRef must be a short reference, not text")
            record["transcriptRef"] = reference
        identity = {
            "contact": contact_id, "type": kind, "channel": channel, "direction": direction,
            "timestamp": timestamp, "anchor": reference or hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        }
        interaction_id = "i-" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:24]
        logged = int(time.time()) if now is None else now
        with self._locked() as state:
            if contact_id != UNMATCHED and contact_id not in state["contacts"]:
                raise ContactError("unknown contact; log against 'unmatched' for a person to assign")
            if interaction_id in state["interactions"]:
                return {
                    "interactionId": interaction_id, "duplicate": True,
                    "externalObjectRef": f"{PROVIDER_ID}:interaction/{interaction_id}",
                }
            if len(state["interactions"]) >= MAX_INTERACTIONS:
                raise ContactError("the interaction log is full; a person must archive or erase records")
            state["interactions"][interaction_id] = {"id": interaction_id, "contact": contact_id, "loggedAt": logged, **record}
        return {
            "interactionId": interaction_id, "duplicate": False,
            "matched": contact_id != UNMATCHED,
            "externalObjectRef": f"{PROVIDER_ID}:interaction/{interaction_id}",
        }

    # -- reads ----------------------------------------------------------------------------

    def read(
        self,
        *,
        object_id: str | None = None,
        query: str | None = None,
        principal_access: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Return records only for a verified staff principal; everyone else gets none."""
        if principal_access != "member":
            return {"records": [], "reason": "requires-verified-staff-principal"}
        limit = max(1, min(int(limit), MAX_READ))
        with self._locked() as state:
            if object_id is not None:
                contact = state["contacts"].get(object_id)
                found = [contact] if contact else []
            elif query is not None:
                needle = _text(query, "query", MAX_QUERY)
                found = self._match(state, needle)[:limit]
            else:
                raise ContactError("read needs an objectId or a query")
            records = [self._public(state, contact) for contact in found]
            if object_id == UNMATCHED:
                records = [{
                    "objectType": "contact", "objectId": UNMATCHED, "fields": {},
                    "interactions": self._interactions(state, UNMATCHED),
                }]
        return {"records": records} if records else {"records": [], "reason": "no-matching-record"}

    @staticmethod
    def _match(state: Mapping[str, Any], needle: str) -> list[dict[str, Any]]:
        lowered = needle.lower()
        digits = re.sub(r"\D", "", needle)
        matches = []
        for contact in state["contacts"].values():
            fields, keys = contact["fields"], contact.get("keys", {})
            if "@" in needle:
                hit = lowered in keys.get("email", "")
            elif len(digits) >= 4 and len(digits) >= len(needle.replace(" ", "")) - 2:
                hit = bool(keys.get("phone")) and keys["phone"].endswith(digits)
            else:
                hit = lowered in fields.get("name", "").lower()
            if hit:
                matches.append(contact)
        return sorted(matches, key=lambda item: (item["createdAt"], item.get("seq", 0), item["id"]))

    @staticmethod
    def _interactions(state: Mapping[str, Any], contact_id: str) -> list[dict[str, Any]]:
        items = [item for item in state["interactions"].values() if item["contact"] == contact_id]
        items.sort(key=lambda item: (item["timestamp"], item["id"]), reverse=True)
        return [
            {key: item[key] for key in ("id", "type", "channel", "direction", "timestamp", "summary", "outcome", "transcriptRef") if key in item}
            for item in items[:MAX_INTERACTIONS_PER_READ]
        ]

    def _public(self, state: Mapping[str, Any], contact: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "objectType": "contact",
            "objectId": contact["id"],
            "fields": dict(contact["fields"]),
            "identityEvidence": contact["identityEvidence"],
            "interactions": self._interactions(state, contact["id"]),
        }

    # -- matching for the platform's own filing step (never reachable from the model) -----

    def find_by_email(self, address: str) -> list[str]:
        """IDs of contacts holding exactly this address. For filing a *verified* sender's conversation only.

        This is internal to the filing step: it is not a Tool, nothing it returns is shown to a caller or
        sender, and a caller-supplied address must never reach it (use the unmatched queue instead).
        """
        needle = normalize_email(address) if isinstance(address, str) else None
        if needle is None:
            return []
        with self._locked() as state:
            return sorted(
                contact_id for contact_id, contact in state["contacts"].items()
                if contact.get("keys", {}).get("email") == needle
            )

    def has_reference(self, reference: str) -> bool:
        """Whether an interaction with this transcript/message reference is already filed anywhere."""
        with self._locked() as state:
            return any(item.get("transcriptRef") == reference for item in state["interactions"].values())

    # -- human review and erasure (operator surface; never reachable from the model) -------

    def review_queue(self) -> dict[str, Any]:
        with self._locked() as state:
            unmatched = [
                {key: item[key] for key in ("id", "type", "channel", "direction", "timestamp", "summary", "transcriptRef") if key in item}
                for item in state["interactions"].values() if item["contact"] == UNMATCHED
            ]
            return {
                "possibleDuplicates": [dict(item) for item in state["review"]],
                "unmatchedInteractions": sorted(unmatched, key=lambda item: item["timestamp"]),
                "contacts": len(state["contacts"]),
                "interactions": len(state["interactions"]),
            }

    def assign_interaction(self, interaction_id: str, contact_id: str) -> dict[str, Any]:
        """A person attaches an unmatched interaction to a contact."""
        with self._locked() as state:
            item = state["interactions"].get(interaction_id)
            if item is None or item["contact"] != UNMATCHED:
                raise ContactError("no such unmatched interaction")
            if contact_id not in state["contacts"]:
                raise ContactError("unknown contact")
            item["contact"] = contact_id
        return {"interactionId": interaction_id, "contactId": contact_id}

    def dismiss_review(self, review_id: str) -> dict[str, Any]:
        with self._locked() as state:
            kept = [item for item in state["review"] if item["id"] != review_id]
            if len(kept) == len(state["review"]):
                raise ContactError("no such review item")
            state["review"] = kept
        return {"reviewId": review_id}

    def erase_contact(self, contact_id: str) -> dict[str, int]:
        """Erase a contact, every interaction filed against it and review items that name it."""
        with self._locked() as state:
            if contact_id not in state["contacts"]:
                raise ContactError("unknown contact")
            del state["contacts"][contact_id]
            gone = [key for key, item in state["interactions"].items() if item["contact"] == contact_id]
            for key in gone:
                del state["interactions"][key]
            before = len(state["review"])
            state["review"] = [item for item in state["review"] if contact_id not in item["contacts"]]
            return {"contacts": 1, "interactions": len(gone), "reviewItems": before - len(state["review"])}

    def erase_interaction(self, interaction_id: str) -> dict[str, int]:
        with self._locked() as state:
            if interaction_id not in state["interactions"]:
                raise ContactError("unknown interaction")
            del state["interactions"][interaction_id]
        return {"interactions": 1}
