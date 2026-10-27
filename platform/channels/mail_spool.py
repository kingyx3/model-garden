"""The built-in inbound mailbox, intake side: a private per-client spool that feeds the support mailbox (#77, #84, #109).

A client with no mailbox to connect still needs somewhere for customer email to land and a way for the coworker to read it. This is the
credential-free half of that: a durable spool that takes raw RFC 5322 messages from whatever receives mail for the client's own address
(a mail server's pipe or delivery agent, a catch-all, a test) and hands each one at least once to ``SupportMailbox.handle``. It listens on
no port, speaks no SMTP, sets up no DNS or MX record and holds no credential; the runtime worker (``platform/runtime/worker.py``)
drains it on its clock and purges it on retention. It is not a ``mail.*`` provider (nothing here reads, drafts or sends through it).

- **Only mail for this client.** A message is accepted only when one of its recipient headers (``To``, ``Cc``, ``Delivered-To``,
  ``X-Original-To``, ``Envelope-To``) names one of the client's inbound addresses exactly. Mail for anyone else is *misdelivered*: it is
  dropped, ledgered by reason and size only, and never stored, so one client's spool cannot become another's mailbox.
- **No authority from the spool.** The spool verifies nothing about the sender and grants nothing. Who wrote the message is still decided
  only from the trusted receiving server's ``Authentication-Results`` by the email surface when the handler parses it, so an unverified
  sender is public with no write authority exactly as before.
- **Idempotent.** A message is identified by a digest of its single ``Message-ID``; delivering it again (an MTA retry) returns the first
  record and stores nothing new. A message with no usable ``Message-ID`` is rejected, because it could not be de-duplicated.
  An acceptance ledger failure leaves a durable ``auditPending`` marker: the worker cannot claim that mail until a matching
  retry records its acceptance. The MTA must retry temporary failures. If a ledger append succeeded but its acknowledgement
  was interrupted, the receipt may be emitted again with the same id (at-least-once, not exactly-once).
- **Private and bounded.** Messages are written as ``<root>/<tenant>/messages/<id>.eml`` (mode 0600, directory 0700) named by digest,
  never by anything in the message; at most 2 MB each and 5,000 per client; a full spool refuses new mail as a temporary failure so the
  sender's server retries instead of mail being lost. The index and the ledger hold identifiers, sizes, states and error types only:
  never an address, subject or text.
- **At least once, never in a loop.** ``claim`` leases messages (default five minutes); a lease that expires makes the message available
  again. A handler that raises records only the error type and the message is retried later with backoff; after three failed attempts it
  is quarantined as ``dead`` and left for a person (``requeue`` is the explicit way back). A handled message is ``done`` and is never
  claimed again.
- **Retention.** Every message, whatever its state, is deleted after the client's retention period (default 30 days, 1 to 365) by
  ``purge``, which records what it removed.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import email
import email.policy
import email.utils
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_MESSAGES = 5000
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365
DEFAULT_LEASE_SECONDS = 300
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 60
STATES = ("new", "leased", "done", "dead")
RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ADDRESS = re.compile(r"^[^@\s<>,;\"]{1,64}@[A-Za-z0-9.-]{1,190}$")
_MESSAGE_ID = re.compile(r"<[^<>\s]{1,250}>")
_ID = re.compile(r"^m-[0-9a-f]{24}$")
_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
Audit = Callable[[str, str, dict[str, Any]], Any]
IDENTITY = "runtime:mail-spool"


class SpoolError(ValueError):
    """Raised for a misconfigured spool or an invalid operation; never carries message contents."""


class SpoolFull(SpoolError):
    """The spool cannot take more mail right now; the sender's server should retry later."""


def _address(value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS.match(value.strip()):
        raise SpoolError("an inbound address must look like support@client.example")
    return value.strip().lower()


class MailSpool:
    def __init__(
        self,
        root: pathlib.Path,
        *,
        tenant: str,
        addresses: Iterable[str],
        retention_days: int = DEFAULT_RETENTION_DAYS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        audit: Audit | None = None,
    ) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise SpoolError("tenant must be a lowercase client-environment slug such as acme-prod")
        listed = tuple(dict.fromkeys(_address(item) for item in addresses))
        if not listed or len(listed) > 20:
            raise SpoolError("a spool needs between one and twenty inbound addresses")
        for name, value, low, high in (("retention", retention_days, 1, MAX_RETENTION_DAYS), ("lease", lease_seconds, 30, 3600),
                                       ("attempts", max_attempts, 1, 10)):
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise SpoolError(f"{name} must be an integer from {low} to {high}")
        self.tenant = tenant
        self.addresses = frozenset(listed)
        self.directory = pathlib.Path(root) / tenant
        self.messages = self.directory / "messages"
        self.index = self.directory / "index.json"
        self._retention = retention_days * 86400
        self._lease = lease_seconds
        self._attempts = max_attempts
        self._audit = audit

    # -- storage -------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.messages.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.directory.parent, self.directory, self.messages):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        descriptor = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "messages": {}}
            if self.index.is_file():
                loaded = json.loads(self.index.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise SpoolError("spool index has an unsupported format or belongs to another client")
                state = loaded
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                temporary = self.index.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(state, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.index)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _record(self, action: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, IDENTITY, details)

    def _acknowledge_acceptance(self, message_id: str, digest: str, size: int) -> None:
        """Ledger a persisted intake before allowing the worker to claim it.

        The ledger and spool are separate durable stores, so an interrupted
        acknowledgement is deliberately retried at least once with the same id.
        Never clear a pending marker when no ledger sink is configured.
        """
        if self._audit is None:
            raise SpoolError("a pending intake requires an audit sink")
        self._record("mail-spool.accepted", {"id": message_id, "bytes": size})
        with self._locked() as state:
            entry = state["messages"].get(message_id)
            if entry is None or entry.get("sha256") != digest:
                raise SpoolError("pending intake changed before audit acknowledgement")
            entry["auditPending"] = False

    def _file(self, message_id: str) -> pathlib.Path:
        if not isinstance(message_id, str) or not _ID.match(message_id):
            raise SpoolError("a spool message id looks like m-0123456789abcdef01234567")
        return self.messages / f"{message_id}.eml"

    # -- intake --------------------------------------------------------------------------

    def _recipients(self, message: email.message.Message) -> set[str]:
        found: set[str] = set()
        for header in RECIPIENT_HEADERS:
            for value in message.get_all(header, []):
                for _, address in email.utils.getaddresses([str(value)]):
                    if address:
                        found.add(address.strip().lower())
        return found

    def ingest(self, raw: bytes, *, now: int | None = None) -> dict[str, Any]:
        """Accept one raw message. ``status`` is ``accepted``, ``duplicate``, ``misdelivered`` or ``rejected``.

        Misdelivered and rejected mail is dropped (and ledgered by reason and size) rather than stored; a full spool raises
        :class:`SpoolFull` so the sending server retries.
        """
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return self._dropped("rejected", "empty", 0)
        raw = bytes(raw)
        if len(raw) > MAX_MESSAGE_BYTES:
            return self._dropped("rejected", "too-large", len(raw))
        message = email.message_from_bytes(raw, policy=email.policy.compat32)  # raw header text, so a header cannot hide a second id
        if not (self._recipients(message) & self.addresses):
            return self._dropped("misdelivered", "not-addressed-to-this-client", len(raw))
        headers = message.get_all("Message-ID", [])
        found = _MESSAGE_ID.findall(str(headers[0])) if len(headers) == 1 else []
        if len(found) != 1:
            return self._dropped("rejected", "no-single-message-id", len(raw))
        message_id = "m-" + hashlib.sha256(found[0].encode("utf-8")).hexdigest()[:24]
        digest = hashlib.sha256(raw).hexdigest()
        duplicate = False
        with self._locked() as state:
            messages = state["messages"]
            if message_id in messages:
                duplicate = True
                existing = messages[message_id]
                pending = existing.get("auditPending", False)
                # A different payload with the same Message-ID must never
                # acknowledge an earlier, unaudited intake.
                if pending and existing.get("sha256") != digest:
                    raise SpoolError("pending intake retry has a different payload")
            else:
                if len(messages) >= MAX_MESSAGES:
                    raise SpoolFull("the spool is full; the sender should retry later")
                path = self._file(message_id)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                pending = self._audit is not None
                messages[message_id] = {
                    "id": message_id, "state": "new", "receivedAt": stamp, "bytes": len(raw), "sha256": digest,
                    "attempts": 0, "availableAt": stamp, "auditPending": pending,
                }
        if pending:
            self._acknowledge_acceptance(message_id, digest, len(raw))
        return {"status": "duplicate" if duplicate else "accepted", "id": message_id}

    def _dropped(self, status: str, reason: str, size: int) -> dict[str, Any]:
        self._record(f"mail-spool.{status}", {"reason": reason, "bytes": size})
        return {"status": status, "reason": reason}

    # -- processing ----------------------------------------------------------------------

    def claim(self, *, limit: int = 10, now: int | None = None) -> list[dict[str, Any]]:
        """Lease up to ``limit`` available messages (oldest first); each comes back as ``{"id", "raw", "attempt"}``."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise SpoolError("limit must be an integer from 1 to 100")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        claimed: list[dict[str, Any]] = []
        with self._locked() as state:
            ready = sorted(
                (entry for entry in state["messages"].values()
                 if not entry.get("auditPending", False) and (
                     (entry["state"] == "new" and entry["availableAt"] <= stamp)
                     or (entry["state"] == "leased" and entry["leaseUntil"] <= stamp))),
                key=lambda entry: (entry["receivedAt"], entry["id"]),
            )
            for entry in ready[:limit]:
                if entry["state"] == "leased" and entry["attempts"] >= self._attempts:
                    # A handler that took the message and never reported back (a crash) counts as a failed attempt, so a
                    # message that kills its handler is quarantined instead of being retried for ever.
                    entry["state"], entry["lastError"] = "dead", "LeaseExpired"
                    entry.pop("leaseUntil", None)
                    continue
                try:
                    raw = self._file(entry["id"]).read_bytes()
                except OSError:
                    entry["state"], entry["lastError"] = "dead", "MissingFile"
                    continue
                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    entry["state"], entry["lastError"] = "dead", "DigestMismatch"
                    continue
                entry.update(state="leased", leaseUntil=stamp + self._lease, attempts=entry["attempts"] + 1)
                claimed.append({"id": entry["id"], "raw": raw, "attempt": entry["attempts"]})
        return claimed

    def complete(self, message_id: str, *, outcome: str = "handled", now: int | None = None) -> dict[str, Any]:
        """Mark a leased message handled. Only a message that is currently leased can be completed."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        label = outcome if isinstance(outcome, str) and _ERROR.match(outcome.replace("-", "_")) else "handled"
        with self._locked() as state:
            entry = state["messages"].get(message_id)
            if entry is None or entry["state"] != "leased":
                raise SpoolError("only a leased message can be completed")
            entry.update(state="done", completedAt=stamp, outcome=label)
            entry.pop("leaseUntil", None)
        self._record("mail-spool.done", {"id": message_id, "outcome": label})
        return {"id": message_id, "state": "done"}

    def fail(self, message_id: str, *, error_type: str, now: int | None = None) -> dict[str, Any]:
        """Record a failed attempt (the error *type* only). Retried with backoff; quarantined as ``dead`` after the last attempt."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        error = error_type if isinstance(error_type, str) and _ERROR.match(error_type) else "Error"
        with self._locked() as state:
            entry = state["messages"].get(message_id)
            if entry is None or entry["state"] != "leased":
                raise SpoolError("only a leased message can be failed")
            entry.pop("leaseUntil", None)
            entry["lastError"] = error
            if entry["attempts"] >= self._attempts:
                entry["state"] = "dead"
            else:
                entry.update(state="new", availableAt=stamp + RETRY_BACKOFF_SECONDS * 2 ** (entry["attempts"] - 1))
            result = {"id": message_id, "state": entry["state"], "attempts": entry["attempts"]}
        self._record("mail-spool.failed", {**result, "errorType": error})
        return result

    def drain(self, handler: Callable[[bytes], Any], *, limit: int = 10, now: int | None = None) -> dict[str, int]:
        """Hand each claimed message to ``handler(raw)`` (for example ``lambda raw: mailbox.handle(raw, now=...)``).

        A return completes the message (the handler's ``status``, if it returns a mapping, is recorded as the outcome); an exception
        fails it with only the error type recorded. Returns counts, never contents.
        """
        counts = {"claimed": 0, "done": 0, "retry": 0, "dead": 0}
        for item in self.claim(limit=limit, now=now):
            counts["claimed"] += 1
            try:
                result = handler(item["raw"])
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the rest or reveal itself
                outcome = self.fail(item["id"], error_type=type(exc).__name__, now=now)
                counts["dead" if outcome["state"] == "dead" else "retry"] += 1
                continue
            status = result.get("status") if isinstance(result, dict) else None
            self.complete(item["id"], outcome=status if isinstance(status, str) else "handled", now=now)
            counts["done"] += 1
        return counts

    def requeue(self, message_id: str, *, by: str, now: int | None = None) -> dict[str, Any]:
        """A person's explicit decision to try a quarantined message again (the attempt count starts over)."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        if not isinstance(by, str) or not by.strip():
            raise SpoolError("requeue needs the person who decided it")
        with self._locked() as state:
            entry = state["messages"].get(message_id)
            if entry is None or entry["state"] != "dead":
                raise SpoolError("only a quarantined message can be requeued")
            entry.update(state="new", attempts=0, availableAt=stamp)
            entry.pop("lastError", None)
        if self._audit is not None:
            self._audit("mail-spool.requeued", by.strip()[:128], {"id": message_id})
        return {"id": message_id, "state": "new"}

    # -- reads and retention ---------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Counts by state and the quarantined ids; never message contents or addresses."""
        with self._locked() as state:
            entries = list(state["messages"].values())
        counts = {name: sum(1 for entry in entries if entry["state"] == name) for name in STATES}
        return {"tenant": self.tenant, "total": len(entries), **counts,
                "auditPending": sum(1 for entry in entries if entry.get("auditPending", False)),
                "deadIds": sorted(entry["id"] for entry in entries if entry["state"] == "dead")}

    def purge(self, *, now: int | None = None) -> dict[str, int]:
        """Delete every message older than the retention period, whatever its state."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        removed = 0
        with self._locked() as state:
            for message_id in [key for key, entry in state["messages"].items() if entry["receivedAt"] + self._retention <= stamp]:
                with contextlib.suppress(FileNotFoundError):
                    self._file(message_id).unlink()
                del state["messages"][message_id]
                removed += 1
        if removed:
            self._record("mail-spool.purged", {"removed": removed})
        return {"removed": removed}
