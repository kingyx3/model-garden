"""Verified data-subject access and correction requests for Model Garden's own Tier 0 stores (#241).

One auditable operator flow for a customer who asks what a client holds about them, built on the stores the runtime already has
(the contact log and the cross-channel conversation timeline). It is deliberately narrow:

- **Two named people, and one customer.** An export needs the person who verified the requester's identity (and how: ``method``) and a
  different person who approved it, plus a reason. The customer is a contact id, or one exact email address or phone number that
  matches exactly one contact; no match or several matches refuse the request (several are never merged or guessed between).
- **A time-bounded bundle.** The export is a JSON file (0600) naming its tenant, its generation time, its expiry (seven days) and what
  it contains and leaves out; ``purge_expired`` removes an expired bundle. A retried request with the same people, customer and reason
  is the same request: it rewrites the same file and ledgers nothing twice.
- **Honest exclusions.** The bundle holds the contact's own fields, every interaction filed against it (free text with email addresses,
  links, long identifiers and phone-like numbers masked so a third party named in a note is not handed over) and the references of
  the conversations linked to it. It does not hold transcripts, memory, tickets, files or anything in an external CRM or messaging
  provider, and says so in ``exclusions``; those need their own handling (#115, #107).
- **Corrections are requests.** ``request_correction`` records which field the customer says is wrong, who verified and approved it and
  its status (``open``, ``applied``, ``declined``); a person makes the change in the source of truth and resolves the request. The ledger
  records ids, field names, counts, people and a digest of the bundle, never a value.

Nothing here has live-deployment proof, and it is not legal advice: what a client must disclose or correct under its own policy is the
business owner's decision.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Iterator
from typing import Any

SCHEMA_VERSION = 1
BUNDLE_DAYS = 7
METHODS = ("email-link", "known-account", "id-document", "call-back", "other")
CORRECTABLE = ("name", "phone", "email", "notes")
STATUSES = ("open", "applied", "declined")
MAX_REQUESTS = 500
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_CONTACT = re.compile(r"^c-[0-9a-f]{16}$")
_REQUEST = re.compile(r"^sa-[0-9a-f]{12}$")
_MASKS = ((re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"), (re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE), "[link]"),
          (re.compile(r"\b[A-Za-z0-9_-]{16,}\b"), "[id]"), (re.compile(r"\+?\d[\d\s().-]{5,}\d"), "[number]"))
Audit = Callable[[str, str, dict[str, Any]], Any]


class SubjectAccessError(ValueError):
    """The request is malformed, unverified, ambiguous or unknown; the message is safe to show an operator."""


def _person(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PERSON.match(value.strip()):
        raise SubjectAccessError(f"{label} must name a person")
    return value.strip()


def _masked(text: Any) -> str:
    out = str(text or "")
    for pattern, label in _MASKS:
        out = pattern.sub(label, out)
    return " ".join(out.split())[:600]


def resolve(contacts: Any, *, contact_id: str | None = None, email: str | None = None, phone: str | None = None) -> str:
    """The one contact an exact id, email or phone names; refuses no match, several matches or a request that names more than one thing."""
    given = [value for value in (contact_id, email, phone) if value]
    if len(given) != 1:
        raise SubjectAccessError("name the customer by exactly one of a contact id, an email address or a phone number")
    if contact_id:
        if not _CONTACT.match(contact_id):
            raise SubjectAccessError("a contact id looks like c-0123456789abcdef")
        found = [contact_id] if contacts.has_contact(contact_id) else []
    elif email:
        found = contacts.find_by_email(email)
    else:
        found = contacts.find_by_phone(phone)
    if not found:
        raise SubjectAccessError("no contact matches that exactly; nothing was exported")
    if len(found) > 1:
        raise SubjectAccessError(f"{len(found)} contacts match; a person must settle which is the customer before anything is exported")
    return found[0]


class SubjectAccess:
    def __init__(self, root: str | os.PathLike[str], *, tenant: str, contacts: Any, conversations: Any | None = None, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", tenant):
            raise SubjectAccessError("a tenant is a short lowercase slug")
        self.root = pathlib.Path(root)
        self.tenant = tenant
        self.contacts, self.conversations, self._audit = contacts, conversations, audit
        self.registry = self.root / f"{tenant}.subject-access.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.registry.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "exports": {}, "corrections": {}}
            if self.registry.is_file():
                loaded = json.loads(self.registry.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise SubjectAccessError("the subject-access registry has an unsupported format")
                state = loaded
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                temporary = self.registry.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(state, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.registry)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _note(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    @staticmethod
    def _check_people(verified_by: Any, approved_by: Any, method: Any, reason: Any) -> tuple[str, str, str, str]:
        verified, approved = _person(verified_by, "verified_by"), _person(approved_by, "approved_by")
        if verified.lower() == approved.lower():
            raise SubjectAccessError("the person who verified the requester and the person who approved the request must be different people")
        if method not in METHODS:
            raise SubjectAccessError("method is how the requester was verified: " + ", ".join(METHODS))
        if not isinstance(reason, str) or not reason.strip():
            raise SubjectAccessError("a reason is required")
        return verified, approved, method, " ".join(reason.split())[:200]

    def export(self, out_dir: str | os.PathLike[str], *, verified_by: str, approved_by: str, method: str, reason: str, now: int,
               contact_id: str | None = None, email: str | None = None, phone: str | None = None) -> dict[str, Any]:
        verified, approved, method, reason = self._check_people(verified_by, approved_by, method, reason)
        subject = resolve(self.contacts, contact_id=contact_id, email=email, phone=phone)
        request_id = "sa-" + hashlib.sha256(f"{self.tenant}|{subject}|{verified}|{approved}|{reason}".encode("utf-8")).hexdigest()[:12]
        record = self.contacts.export_subject(subject)
        linked = self.conversations.customer_timeline(subject) if self.conversations is not None else []
        bundle = {
            "schemaVersion": SCHEMA_VERSION, "kind": "subject-access-export", "tenant": self.tenant, "requestId": request_id,
            "generatedAt": int(now), "expiresAt": int(now) + BUNDLE_DAYS * 86_400, "verifiedBy": verified, "approvedBy": approved,
            "verificationMethod": method,
            "subject": {"id": subject, "fields": record["fields"], "identityEvidence": record["identityEvidence"], "createdAt": record["createdAt"]},
            "interactions": [{**item, "summary": _masked(item.get("summary"))} for item in record["interactions"]],
            "conversations": [{"conversationId": row["conversationId"], "channel": row["channel"], "at": row.get("at"), "kind": row.get("kind")}
                              for row in linked],
            "exclusions": ["call and message transcripts", "agent memory", "tickets and uploaded files", "external CRM and messaging-provider data",
                           "free-text email addresses, links, identifiers and phone-like numbers in notes are masked"],
        }
        document = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
        directory = pathlib.Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{request_id}.json"
        digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
        with self._locked() as state:
            existing = state["exports"].get(request_id)
            fresh = existing is None or existing["expiresAt"] <= int(now) or not path.is_file()
            temporary = path.with_suffix(".tmp")
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            state["exports"][request_id] = {"requestId": request_id, "contact": subject, "path": path.name, "expiresAt": bundle["expiresAt"],
                                            "generatedAt": int(now), "digest": digest}
            if len(state["exports"]) > MAX_REQUESTS:
                for old in sorted(state["exports"].values(), key=lambda item: item["generatedAt"])[: len(state["exports"]) - MAX_REQUESTS]:
                    del state["exports"][old["requestId"]]
        if fresh:
            self._note("subject-access.exported", approved, {"requestId": request_id, "contact": subject, "verifiedBy": verified, "approvedBy": approved,
                                                           "method": method, "interactions": len(bundle["interactions"]),
                                                           "conversations": len(bundle["conversations"]), "digest": digest[:16], "expiresAt": bundle["expiresAt"]})
        return {"requestId": request_id, "path": str(path), "expiresAt": bundle["expiresAt"], "interactions": len(bundle["interactions"]),
                "conversations": len(bundle["conversations"]), "repeat": not fresh}

    def purge_expired(self, out_dir: str | os.PathLike[str], *, now: int, by: str = "runtime:retention") -> int:
        removed = 0
        with self._locked() as state:
            for request_id, item in list(state["exports"].items()):
                if item["expiresAt"] > int(now):
                    continue
                with contextlib.suppress(FileNotFoundError):
                    (pathlib.Path(out_dir) / item["path"]).unlink()
                del state["exports"][request_id]
                removed += 1
        if removed:
            self._note("subject-access.bundle-purged", by, {"bundles": removed})
        return removed

    def request_correction(self, *, contact_id: str, field: str, verified_by: str, approved_by: str, method: str, reason: str, now: int) -> dict[str, Any]:
        verified, approved, method, reason = self._check_people(verified_by, approved_by, method, reason)
        subject = resolve(self.contacts, contact_id=contact_id)
        if field not in CORRECTABLE:
            raise SubjectAccessError("the field to correct is one of " + ", ".join(CORRECTABLE))
        request_id = "sa-" + hashlib.sha256(f"correction|{self.tenant}|{subject}|{field}|{verified}|{approved}|{reason}".encode("utf-8")).hexdigest()[:12]
        with self._locked() as state:
            existing = state["corrections"].get(request_id)
            if existing is not None:
                return {**existing, "repeat": True}
            entry = {"requestId": request_id, "contact": subject, "field": field, "status": "open", "verifiedBy": verified, "approvedBy": approved,
                     "method": method, "reason": reason, "requestedAt": int(now)}
            state["corrections"][request_id] = entry
        self._note("subject-access.correction-requested", approved, {"requestId": request_id, "contact": subject, "field": field, "verifiedBy": verified})
        return {**entry, "repeat": False}

    def resolve_correction(self, request_id: str, status: str, *, by: str, now: int) -> dict[str, Any]:
        if status not in ("applied", "declined"):
            raise SubjectAccessError("a correction is resolved as applied or declined")
        if not isinstance(request_id, str) or not _REQUEST.match(request_id):
            raise SubjectAccessError("a request id looks like sa-0123456789ab")
        person = _person(by, "by")
        with self._locked() as state:
            entry = state["corrections"].get(request_id)
            if entry is None:
                raise SubjectAccessError("no such correction request")
            if entry["status"] != "open":
                raise SubjectAccessError(f"that request is already {entry['status']}")
            entry.update(status=status, resolvedBy=person, resolvedAt=int(now))
            result = dict(entry)
        self._note("subject-access.correction-resolved", person, {"requestId": request_id, "contact": result["contact"], "field": result["field"], "status": status})
        return result

    def corrections(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self._locked() as state:
            items = [dict(item) for item in state["corrections"].values() if status is None or item["status"] == status]
        return sorted(items, key=lambda item: (item["requestedAt"], item["requestId"]))
