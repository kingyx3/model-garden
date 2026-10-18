"""One-time, owner-only secret entry that never puts a secret in a chat, a ledger or a file we own (#159, #86).

A client owner has to give a coworker's connection a credential (an API key, a bearer token, a client secret) without pasting it
into a prompt, a Skill file or an issue. This is the credential-free core of that: an operator or administrator opens an
**enrollment** for one coworker's connection, the owner receives a link containing a single-use token, and the owner submits the
value through whatever page or command the deployment serves. The value goes straight to an injected ``vault`` (``put`` and
``delete`` only; this module can never read a secret back) and what remains is a ``secret://`` *reference*, a version number and
a masked status. The gateway serves the page (``/connections/enroll``), ``runtime-connection.py credential`` opens links, and
``sealed_vault.SealedVault`` is the vault the deployment injects.

- **Single use, short life, owner only.** A token lives at most an hour (default 15 minutes), works once, is stored only as a
  SHA-256 digest, and is bound to one tenant, coworker and connection. Only the named owner can submit (the channel's verified
  identity is the caller's responsibility), and an expired, used, cancelled, unknown or wrong-owner submission is refused and
  ledgered without the token or the value.
- **The value never leaves the vault call.** It is validated (a bounded single line; ``basic`` is ``user:password``), written to the
  vault under a versioned reference, and dropped. The store, the ledger, every return value and every error message exclude it;
  a status shows only ``set``, the version, when and by whom, an expiry and a masked length.
- **Rotation keeps the old one until the new one is in.** A rotation is another enrollment for the same connection. The new
  version is written first and the previous version is deleted only after that succeeds; if the vault refuses, nothing changes and
  the old credential keeps working.
- **Revocation deletes.** ``revoke`` deletes every stored version from the vault and records who did it; ``status`` then reports
  ``revoked``. A credential with an expiry reports ``expiring`` within a warning window and ``expired`` after it, without
  showing anything about the value.
- **Private and bounded.** One file per tenant (0600 in a 0700 directory, atomic writes under a lock; another tenant's file is
  refused), a bounded number of live enrollments, and ``purge_expired`` clears dead ones. Ledger events carry identifiers,
  kinds and versions only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
from collections.abc import Callable, Iterator
from typing import Any, Protocol

SCHEMA_VERSION = 1
KINDS = ("bearer", "api-key", "basic", "oauth-client")
DEFAULT_TTL = 15 * 60
MAX_TTL = 60 * 60
MAX_VALUE = 4096
MAX_LIVE = 200
EXPIRY_WARNING_SECONDS = 7 * 86400
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CONNECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
Audit = Callable[[str, str, dict[str, Any]], Any]


class EnrollmentError(PermissionError):
    """Raised when an enrollment or submission is not allowed; nothing is stored and no secret is written."""


class Vault(Protocol):
    """What this module needs from a secret store: write and delete by reference. It can never read a value back."""

    def put(self, ref: str, value: str) -> None: ...

    def delete(self, ref: str) -> None: ...


def _check(value: Any, pattern: re.Pattern[str], message: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise EnrollmentError(message)
    return value


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class EnrollmentStore:
    def __init__(self, root: pathlib.Path, *, tenant: str, vault: Vault, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise EnrollmentError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.secret-enrollments.json"
        self._vault = vault
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / f".{self.tenant}.secret-enrollments.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "enrollments": {}, "credentials": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise EnrollmentError("enrollment store has an unsupported format or belongs to another tenant")
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

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    @staticmethod
    def _key(agent: str, connection_id: str) -> str:
        return f"{agent}/{connection_id}"

    def _ref(self, agent: str, connection_id: str, version: int) -> str:
        return f"secret://{self.tenant}/{agent}/{connection_id}/v{version}"

    # -- opening an enrollment -----------------------------------------------------------------

    def create(
        self, *, agent: str, connection_id: str, kind: str, owner: str, requested_by: str, now: int, ttl_seconds: int = DEFAULT_TTL
    ) -> dict[str, Any]:
        """Open a single-use enrollment; the returned ``token`` is shown here once and never stored."""
        _check(agent, _AGENT, "choose the specific coworker this credential is for")
        _check(connection_id, _CONNECTION, "connection id must be a short identifier")
        _check(owner, _PERSON, "the owner must be a named person")
        _check(requested_by, _PERSON, "requested_by must be a named person")
        if kind not in KINDS:
            raise EnrollmentError("kind must be one of " + ", ".join(KINDS))
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= MAX_TTL:
            raise EnrollmentError(f"an enrollment lives between 60 and {MAX_TTL} seconds")
        token = secrets.token_urlsafe(32)
        enrollment_id = "en-" + secrets.token_hex(6)
        with self._locked() as state:
            live = [item for item in state["enrollments"].values() if item["state"] == "pending" and item["expiresAt"] > now]
            if len(live) >= MAX_LIVE:
                raise EnrollmentError("too many open enrollments; let them expire or cancel some")
            for item in live:  # one open enrollment per connection: a new one replaces the old, so two links never race
                if (item["agent"], item["connectionId"]) == (agent, connection_id):
                    item["state"] = "cancelled"
            previous = state["credentials"].get(self._key(agent, connection_id))
            version = (previous["version"] if previous else 0) + 1
            state["enrollments"][enrollment_id] = {
                "id": enrollment_id, "tokenDigest": _digest(token), "agent": agent, "connectionId": connection_id, "kind": kind, "owner": owner,
                "requestedBy": requested_by, "createdAt": now, "expiresAt": now + ttl_seconds, "state": "pending", "version": version,
            }
        self._record("secret.enrollment-opened", requested_by, {"enrollmentId": enrollment_id, "agent": agent, "connectionId": connection_id, "kind": kind,
                                                                "owner": owner, "version": version, "expiresAt": now + ttl_seconds})
        return {"enrollmentId": enrollment_id, "token": token, "expiresAt": now + ttl_seconds, "version": version, "kind": kind}

    def cancel(self, enrollment_id: str, *, by: str) -> None:
        _check(by, _PERSON, "by must be a named person")
        with self._locked() as state:
            item = state["enrollments"].get(enrollment_id)
            if item is None or item["state"] != "pending":
                raise EnrollmentError("no such open enrollment")
            item["state"] = "cancelled"
        self._record("secret.enrollment-cancelled", by, {"enrollmentId": enrollment_id})

    def purge_expired(self, *, now: int) -> int:
        with self._locked() as state:
            gone = [key for key, item in state["enrollments"].items() if item["state"] != "pending" or item["expiresAt"] <= now]
            for key in gone:
                del state["enrollments"][key]
        return len(gone)

    # -- submitting the value ------------------------------------------------------------------

    @staticmethod
    def _valid_value(kind: str, value: Any) -> str:
        if (not isinstance(value, str) or not value or len(value) > MAX_VALUE or _CONTROL.search(value) or value != value.strip()
                or not value.isascii()):
            # HTTP headers carry the value, so it must be ASCII.
            raise EnrollmentError(f"the credential is one line of at most {MAX_VALUE} ASCII characters with no leading or trailing space")
        if kind == "basic" and (":" not in value or value.startswith(":")):
            raise EnrollmentError("a basic credential is user:password")
        return value

    def submit(self, token: str, value: str, *, submitted_by: str, now: int, credential_expires_at: int | None = None) -> dict[str, Any]:
        """Store the owner's value in the vault under a new versioned reference; the result holds no secret."""
        _check(submitted_by, _PERSON, "the submitter must be a named person")
        if not isinstance(token, str) or not _TOKEN.match(token):
            self._record("secret.enrollment-rejected", submitted_by, {"reason": "malformed-token"})
            raise EnrollmentError("this link is not valid")
        if credential_expires_at is not None and (not isinstance(credential_expires_at, int) or isinstance(credential_expires_at, bool) or credential_expires_at <= now):
            raise EnrollmentError("a credential expiry must be a future epoch time")
        digest = _digest(token)
        with self._locked() as state:
            match = next((item for item in state["enrollments"].values() if hmac.compare_digest(item["tokenDigest"], digest)), None)
            if match is None:
                reason = "unknown-token"
            elif match["state"] != "pending":
                reason = f"{match['state']}"
            elif match["expiresAt"] <= now:
                match["state"] = "expired"
                reason = "expired"
            elif match["owner"].casefold() != submitted_by.casefold():
                reason = "wrong-person"
            else:
                reason = ""
            if reason:
                details = {"reason": reason, **({"enrollmentId": match["id"]} if match else {})}
            else:
                self._valid_value(match["kind"], value)
                ref = self._ref(match["agent"], match["connectionId"], match["version"])
                key = self._key(match["agent"], match["connectionId"])
                previous = state["credentials"].get(key)
                try:
                    self._vault.put(ref, value)
                except Exception as exc:  # noqa: BLE001 - the old credential stays, the link stays usable, nothing about the value leaks
                    details = {"reason": "vault-refused", "enrollmentId": match["id"], "error": type(exc).__name__}
                    reason = "vault-refused"
                else:
                    match["state"] = "used"
                    record = {"version": match["version"], "ref": ref, "kind": match["kind"], "setAt": now, "setBy": submitted_by, "length": len(value),
                              "expiresAt": credential_expires_at, "revokedAt": None}
                    state["credentials"][key] = record
                    superseded = previous["ref"] if previous and not previous.get("revokedAt") else None
        if reason:
            self._record("secret.enrollment-rejected", submitted_by, details)
            raise EnrollmentError("this link is not valid" if reason != "vault-refused" else "the secret could not be stored; try again")
        if superseded:
            try:
                self._vault.delete(superseded)
            except Exception:  # noqa: BLE001 - surplus old version is reported by the caller's vault audit, never hidden or fatal
                self._record("secret.cleanup-failed", submitted_by, {"agent": match["agent"], "connectionId": match["connectionId"], "version": previous["version"]})
        self._record("secret.set", submitted_by, {"agent": match["agent"], "connectionId": match["connectionId"], "kind": match["kind"], "version": match["version"],
                                                  "rotated": superseded is not None})
        return {"ref": ref, "version": match["version"], "kind": match["kind"], "masked": f"set ({len(value)} characters)", "expiresAt": credential_expires_at}

    # -- status, revocation --------------------------------------------------------------------

    def status(self, agent: str, connection_id: str, *, now: int) -> dict[str, Any]:
        """Whether a credential is set, with a mask: never a value, a prefix, a suffix or a digest of it."""
        with self._locked() as state:
            record = state["credentials"].get(self._key(agent, connection_id))
        if record is None:
            return {"state": "unset"}
        if record.get("revokedAt"):
            return {"state": "revoked", "version": record["version"], "revokedAt": record["revokedAt"]}
        expiry = record.get("expiresAt")
        health = "expired" if expiry is not None and expiry <= now else "expiring" if expiry is not None and expiry - now <= EXPIRY_WARNING_SECONDS else "ok"
        return {"state": "set", "health": health, "version": record["version"], "kind": record["kind"], "setAt": record["setAt"], "setBy": record["setBy"],
                "masked": f"set ({record['length']} characters)", "expiresAt": expiry, "ref": record["ref"]}

    def revoke(self, agent: str, connection_id: str, *, by: str, now: int) -> dict[str, Any]:
        """Delete the stored credential and every open enrollment for the connection."""
        _check(by, _PERSON, "by must be a named person")
        with self._locked() as state:
            record = state["credentials"].get(self._key(agent, connection_id))
            if record is None or record.get("revokedAt"):
                raise EnrollmentError("there is no credential set for that connection")
            for item in state["enrollments"].values():
                if (item["agent"], item["connectionId"]) == (agent, connection_id) and item["state"] == "pending":
                    item["state"] = "cancelled"
            ref = record["ref"]
        try:
            self._vault.delete(ref)
        except Exception as exc:  # noqa: BLE001 - not revoked until the vault says so: leave the record as it was and say why
            self._record("secret.revoke-failed", by, {"agent": agent, "connectionId": connection_id, "error": type(exc).__name__})
            raise EnrollmentError("the vault did not delete the credential; it is still stored") from exc
        with self._locked() as state:
            state["credentials"][self._key(agent, connection_id)]["revokedAt"] = now
        self._record("secret.revoked", by, {"agent": agent, "connectionId": connection_id, "version": record["version"]})
        return {"state": "revoked", "version": record["version"]}
