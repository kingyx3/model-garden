"""Notices to a connection's owner when a connection or provider grant needs a person.

Today a failed or revoked connection is enforced at the governed boundary and shown in the inbox, but nothing tells the
named owner (#86, #77). This module works out *who should be told what, and when*, so a channel or the inbox can deliver
it. It sends nothing itself: a deployment delivers each notice and then records that it did, exactly like hand-off
notices (``handoff.due_notices`` / ``mark_notified``). It is credential-free; the runtime worker (``platform/runtime/worker.py``)
delivers its notices through ``notice_delivery.py`` to the owner's channel.

- **Conditions.** A capability connection that is ``degraded``, ``failed`` or ``revoked`` (``capabilities.read_connection_
  health``), and a per-coworker provider grant (``provider_grants.py``, #129) that is revoked, failed, degraded, expired or
  expiring within a warning window (default 7 days).
- **Who.** The connection's named ``owner`` from the EnvironmentBinding, or the grant's owner. A connection with no named owner
  goes to the escalation contact when one is given; with neither, the notice is still returned with ``to: None`` and
  ``unowned: true`` so it can never be silently dropped.
- **Once per state, at least once.** Each notice has a stable key from the connection or grant, the condition and when the state
  began. A notice is offered until delivery is recorded and is then not repeated for the unchanged state. When the condition
  clears the record is forgotten, so a later recurrence is told again.
- **Non-secret.** A notice names the capability family or the grant's connection, the condition and the recovery step. It never
  carries a reason string, scope, account, secret reference or credential.
- **Only offered notices can be marked.** Delivery can be recorded only for a key this store offered, so a stray caller cannot
  silence a notice that was never produced.
- **Told is not seen.** The named owner (or the escalation contact) can :meth:`NoticeStore.acknowledge` a delivered notice, which is
  ledgered with who and through which channel. A delivered notice nobody acknowledges within the window (default 24 hours) is
  escalated once to the escalation contact as its own notice, so a failed connection cannot sit unnoticed because a message went
  to a mailbox no one reads. An acknowledgement only stops the escalation: it does not mark the connection fixed, and it is not
  accepted from anyone else.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_EXPIRY_WARNING_SECONDS = 7 * 86400
DEFAULT_ACKNOWLEDGE_WITHIN_SECONDS = 24 * 3600
CONNECTION_CONDITIONS = ("degraded", "failed", "revoked")
MAX_RECORDS = 2000
_KEY = re.compile(r"^[0-9a-f]{64}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_SHORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
Audit = Callable[[str, str, dict[str, Any]], Any]
_RECOVERY = {
    "failed": "reconnect it (an operator can re-authorise it) and then mark it healthy",
    "revoked": "authorise it again if the access should continue; until then the coworker will not use it",
    "degraded": "check it with your operator; the coworker can still use it but may fall back where it can",
}


class NoticeError(ValueError):
    """Raised for invalid input; never carries notice contents beyond identifiers."""


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps([str(part) for part in parts], separators=(",", ":")).encode("utf-8")).hexdigest()


def _owner(value: Any) -> str | None:
    return value if isinstance(value, str) and _PERSON.match(value) else None


def _recipient(owner: str | None, escalate_to: str | None) -> dict[str, Any]:
    if owner:
        return {"to": owner, "unowned": False}
    return {"to": escalate_to if escalate_to and _PERSON.match(escalate_to) else None, "unowned": True}


def current_notices(
    health: Mapping[str, Mapping[str, Any]],
    connectors: Mapping[str, Mapping[str, Any]],
    grants: Iterable[Mapping[str, Any]] = (),
    *,
    now: int,
    expiry_warning_seconds: int = DEFAULT_EXPIRY_WARNING_SECONDS,
    escalate_to: str | None = None,
) -> list[dict[str, Any]]:
    """Every notice that applies right now, whether or not it was already delivered (pure; no state)."""
    if not isinstance(expiry_warning_seconds, int) or isinstance(expiry_warning_seconds, bool) or expiry_warning_seconds < 0:
        raise NoticeError("the expiry warning window must be a non-negative number of seconds")
    notices: list[dict[str, Any]] = []
    for family, record in sorted(health.items()):
        condition = record.get("health")
        if condition not in CONNECTION_CONDITIONS or not _SHORT.match(str(family)):
            continue
        entry = connectors.get(family) if isinstance(connectors.get(family), Mapping) else {}
        started = str(record.get("updatedAt", ""))[:40]
        notices.append({
            "key": _key("connection", family, condition, started), "kind": f"connection-{condition}", "family": family,
            "text": f"The {family} connection is {condition}. To fix it, {_RECOVERY[condition]}.",
            **_recipient(_owner(entry.get("owner")), escalate_to),
        })
    for grant in grants:
        agent, connection, owner = str(grant.get("agent", "")), str(grant.get("connectionId", "")), _owner(grant.get("owner"))
        if not (_SHORT.match(agent) and _SHORT.match(connection)):
            continue
        expires = grant.get("expiresAt")
        conditions: list[tuple[str, str, str]] = []
        if grant.get("revokedAt"):
            conditions.append(("grant-revoked", str(grant["revokedAt"]), f"{agent}'s access through {connection} was revoked; authorise it again if it should continue"))
        else:
            if grant.get("health") == "failed":
                conditions.append(("grant-failed", "failed", f"{agent}'s access through {connection} has failed; an operator needs to repair or replace it"))
            elif grant.get("health") == "degraded":
                conditions.append(("grant-degraded", "degraded", f"{agent}'s access through {connection} is degraded; check it with your operator"))
            if isinstance(expires, int) and not isinstance(expires, bool):
                if expires <= now:
                    conditions.append(("grant-expired", str(expires), f"{agent}'s access through {connection} has expired; renew it to keep the coworker working"))
                elif expires - now <= expiry_warning_seconds:
                    conditions.append(("grant-expiring", str(expires), f"{agent}'s access through {connection} expires soon; renew it before it stops"))
        for kind, since, text in conditions:
            notices.append({
                "key": _key("grant", grant.get("id"), kind, since), "kind": kind, "family": grant.get("family"), "agent": agent,
                "connectionId": connection, "text": text + ".", **_recipient(owner, escalate_to),
            })
    return notices


class NoticeStore:
    """Which notices were offered and which delivered; one private file under the governance root."""

    def __init__(self, root: pathlib.Path, *, audit: Audit | None = None) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / "connection-notices.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / ".connection-notices.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "offered": {}, "delivered": {}, "acknowledged": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise NoticeError("notice store has an unsupported format")
                state = loaded
                state.setdefault("acknowledged", {})
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

    def due(
        self,
        health: Mapping[str, Mapping[str, Any]],
        connectors: Mapping[str, Mapping[str, Any]],
        grants: Iterable[Mapping[str, Any]] = (),
        *,
        now: int,
        expiry_warning_seconds: int = DEFAULT_EXPIRY_WARNING_SECONDS,
        escalate_to: str | None = None,
        acknowledge_within_seconds: int = DEFAULT_ACKNOWLEDGE_WITHIN_SECONDS,
    ) -> list[dict[str, Any]]:
        """Notices to deliver now: current, and not yet recorded as delivered. Cleared conditions are forgotten.

        A delivered notice that has not been acknowledged within ``acknowledge_within_seconds`` is also returned, once, as an
        ``…-unacknowledged`` notice to ``escalate_to`` (when one is given and is not already the recipient).
        """
        if not isinstance(acknowledge_within_seconds, int) or isinstance(acknowledge_within_seconds, bool) or acknowledge_within_seconds < 60:
            raise NoticeError("the acknowledgement window must be at least 60 seconds")
        active = current_notices(health, connectors, grants, now=now, expiry_warning_seconds=expiry_warning_seconds, escalate_to=escalate_to)
        keys = {notice["key"] for notice in active}
        keep = keys | {_key("escalation", key) for key in keys}
        contact = escalate_to if isinstance(escalate_to, str) and _PERSON.match(escalate_to) else None
        with self._locked() as state:
            for table in ("delivered", "offered", "acknowledged"):
                state[table] = {key: value for key, value in state[table].items() if key in keep}
            pending = [notice for notice in active if notice["key"] not in state["delivered"]]
            for notice in pending:
                if len(state["offered"]) < MAX_RECORDS:
                    state["offered"][notice["key"]] = {"kind": notice["kind"], "offeredAt": now, "to": notice.get("to")}
            for notice in active:
                told = state["delivered"].get(notice["key"])
                escalation_key = _key("escalation", notice["key"])
                if (
                    contact is None or told is None or notice["key"] in state["acknowledged"]
                    or escalation_key in state["delivered"] or notice.get("to") == contact
                    or now - int(told["at"]) < acknowledge_within_seconds
                ):
                    continue
                kind = f"{notice['kind']}-unacknowledged"
                hours = max(1, acknowledge_within_seconds // 3600)
                pending.append({
                    "key": escalation_key, "kind": kind, "family": notice.get("family"), "to": contact, "unowned": False,
                    "text": f"{notice['text']} The owner was told about this more than {hours} hour(s) ago and has not acknowledged it.",
                    **({"agent": notice["agent"], "connectionId": notice["connectionId"]} if "agent" in notice else {}),
                })
                if len(state["offered"]) < MAX_RECORDS:
                    state["offered"][escalation_key] = {"kind": kind, "offeredAt": now, "to": contact}
        return pending

    def acknowledge(self, key: str, *, by: str, via: str, now: int, escalate_to: str | None = None) -> dict[str, Any]:
        """Record that a person has seen a delivered notice; only its named recipient, or the escalation contact, may.

        Acknowledging stops the escalation of that notice. It never changes the connection's health, and a repeat changes nothing.
        """
        if not isinstance(key, str) or not _KEY.match(key):
            raise NoticeError("a notice key is a 64-character lowercase hex value")
        if not isinstance(via, str) or not _SHORT.match(via):
            raise NoticeError("name the channel the acknowledgement came through, for example slack or inbox")
        if not isinstance(by, str) or not _PERSON.match(by):
            raise NoticeError("an acknowledgement must name the person who gave it")
        with self._locked() as state:
            offered, told = state["offered"].get(key), state["delivered"].get(key)
            if offered is None or told is None:
                raise NoticeError("no delivered notice has that key")
            recipient = offered.get("to")
            if by != recipient and not (isinstance(escalate_to, str) and by == escalate_to):
                raise NoticeError("only the notice's recipient or the escalation contact can acknowledge it")
            if key in state["acknowledged"]:
                return {"key": key, **state["acknowledged"][key], "duplicate": True}
            record = {"kind": offered["kind"], "by": by, "via": via, "at": now}
            state["acknowledged"][key] = record
        if self._audit is not None:
            self._audit("connection.notice-acknowledged", by, {"noticeKey": key, "kind": record["kind"], "via": via})
        return {"key": key, **record, "duplicate": False}

    def mark_notified(self, key: str, *, via: str, now: int) -> dict[str, Any]:
        """Record delivery of a notice this store offered (and through which channel); a repeat changes nothing."""
        if not isinstance(key, str) or not _KEY.match(key):
            raise NoticeError("a notice key is a 64-character lowercase hex value")
        if not isinstance(via, str) or not _SHORT.match(via):
            raise NoticeError("name the delivery channel, for example slack or inbox")
        with self._locked() as state:
            offered = state["offered"].get(key)
            if offered is None:
                raise NoticeError("no such notice was offered")
            if key in state["delivered"]:
                return {"key": key, **state["delivered"][key], "duplicate": True}
            record = {"kind": offered["kind"], "via": via, "at": now}
            state["delivered"][key] = record
        if self._audit is not None:
            self._audit("connection.notice-delivered", "runtime:notices", {"noticeKey": key, "kind": record["kind"], "via": via})
        return {"key": key, **record, "duplicate": False}
