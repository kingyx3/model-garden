"""Deliver hand-off and connection notices to the named person, through the channel *they* use.

``handoff.due_notices`` and ``connection_notices`` work out who should be told what and when, and deliberately send nothing
(#88, #97, #86). This is the credential-free runner that sits between them and a channel: it asks each source for its due
notices, looks the recipient up in a small people directory, sends through the channel registered for that person and only then
records the delivery. It holds no credential and imports no provider; each channel is an injected ``send(address, text) -> ref``
(for example a Slack or Teams adapter's ``send_reply`` wrapped with :func:`adapter_sender`). The runtime worker
(``platform/runtime/worker.py``) runs it on its clock for hand-off, connection, grant, schedule and mailbox notices.

- **The person's channel, not ours.** The directory maps a named person to one channel and one address (for a chat channel, the
  id of the conversation with that person). A person with no entry, or a channel with no registered sender, is reported as
  ``no-channel`` and the notice stays due, so it is offered again once a channel exists. A notice is never marked delivered
  because it was merely *visible* in the inbox; ``inbox`` counts as a channel only when the person's entry says so.
- **At least once, never marked early.** Delivery is recorded after the send succeeds. A send that raises records only the error
  type, leaves the notice due and does not stop the rest of the batch. A failed ``mark`` after a successful send is reported and
  the next run sends again (a duplicate message is the price of never losing a notice).
- **Unowned notices stay loud.** A notice with no recipient is reported as ``unowned`` and ledgered every run; it is never
  dropped.
- **Safe text only.** Sources already exclude arguments, reasons and secrets; this module also bounds the text and refuses
  anything that is not a plain string. The ledger records the source, kind, reference, channel and error type, never the
  message text or the recipient's address.
- **One result per notice.** The report is returned to the caller, so an operator command or a scheduler can show what reached
  whom.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

MAX_TEXT = 1000
MAX_PEOPLE = 500
STATUSES = ("delivered", "failed", "no-channel", "unowned", "unmarked")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_CHANNEL = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_ADDRESS = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_SECRETISH = re.compile(r"secret|token|credential|password|passwd|api[-_]?key|private[-_]?key", re.IGNORECASE)
Sender = Callable[[str, str], Any]
Audit = Callable[[str, str, dict[str, Any]], Any]
Mark = Callable[[Mapping[str, Any], str], Any]


class DeliveryError(ValueError):
    """Raised for an invalid directory or notice source; nothing is sent when it is raised."""


def _sibling(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parent / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


class Directory:
    """Who is reached how: ``{person: {"channel": "slack", "address": "D012345"}}``; strict and secret-free."""

    def __init__(self, people: Mapping[str, Mapping[str, Any]]) -> None:
        if not isinstance(people, Mapping) or len(people) > MAX_PEOPLE:
            raise DeliveryError(f"the people directory is an object of at most {MAX_PEOPLE} people")
        entries: dict[str, tuple[str, str]] = {}
        identities: dict[str, str] = {}
        for person, entry in people.items():
            if not isinstance(person, str) or not _PERSON.match(person):
                raise DeliveryError("a directory person must be a named identity, never a password or token")
            if not isinstance(entry, Mapping) or set(entry) - {"channel", "address", "identity"}:
                raise DeliveryError(f"{person}: an entry holds only channel, address and an optional identity")
            channel, address, identity = entry.get("channel"), entry.get("address", ""), entry.get("identity")
            if not isinstance(channel, str) or not _CHANNEL.match(channel):
                raise DeliveryError(f"{person}: channel must be a short lowercase name such as slack, teams, email or inbox")
            if channel == "inbox":
                address = ""
            elif not isinstance(address, str) or not _ADDRESS.match(address) or _SECRETISH.search(address):
                raise DeliveryError(f"{person}: address must be one line naming where to reach them, never a credential")
            if identity is not None and (not isinstance(identity, str) or not _ADDRESS.match(identity) or _SECRETISH.search(identity)):
                raise DeliveryError(f"{person}: identity must be the channel's own user id for them, never a credential")
            entries[person] = (channel, address)
            if identity is not None:
                identities[person] = identity
        self._entries = entries
        self._identities = identities

    def lookup(self, person: str | None) -> tuple[str, str] | None:
        return self._entries.get(person) if isinstance(person, str) else None

    def identify(self, channel: str, address: str, sender_id: str | None = None) -> str | None:
        """Who an inbound message came from: the one person whose entry is exactly this channel and conversation.

        Used only for a message the channel adapter has already authenticated and that arrived in a direct (one-to-one)
        conversation. When the entry also records the person's own ``identity`` on that channel, ``sender_id`` must match it;
        an address held by two people identifies nobody. ``inbox`` has no address, so it never identifies anyone.
        """
        if not isinstance(channel, str) or not isinstance(address, str) or not address or channel == "inbox":
            return None
        holders = [person for person, entry in self._entries.items() if entry == (channel, address)]
        if len(holders) != 1:
            return None
        wanted = self._identities.get(holders[0])
        if wanted is not None and sender_id != wanted:
            return None
        return holders[0]

    def __len__(self) -> int:
        return len(self._entries)


def adapter_sender(adapter: Any) -> Sender:
    """Wrap a channel adapter's ``send_reply`` as a sender; the address is the conversation with the person."""
    text_channel = _sibling("model_garden_notice_text_channel", "../channels/text_channel.py")

    def send(address: str, text: str) -> Any:
        return adapter.send_reply(text_channel.OutboundReply(conversation_id=address, thread_id=None, text=text))

    return send


def _normalise(source: str, notices: Iterable[Mapping[str, Any]], reference: Callable[[Mapping[str, Any]], str]) -> list[dict[str, Any]]:
    result = []
    for notice in notices:
        text = notice.get("text")
        recipient = notice.get("to")
        if not isinstance(text, str) or not text.strip():
            raise DeliveryError(f"a {source} notice has no text to send")
        ref = reference(notice)
        result.append({
            "source": source, "kind": str(notice.get("kind", ""))[:40], "reference": ref,
            "to": recipient if isinstance(recipient, str) and recipient else None, "text": text.strip()[:MAX_TEXT], "notice": dict(notice),
        })
    return result


def source(
    name: str, notices: Iterable[Mapping[str, Any]], mark: Mark, *, reference: Callable[[Mapping[str, Any]], str]
) -> tuple[list[dict[str, Any]], Mark]:
    """Any other notice producer as a delivery source: ``notices`` carry ``to``, ``text`` and ``kind``; ``mark(item, via)`` records delivery."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
        raise DeliveryError("a source name is a short lowercase word")
    return _normalise(name, notices, reference), mark


def handoff_source(root: pathlib.Path, *, escalate_to: str | None = None, now: int | None = None) -> tuple[list[dict[str, Any]], Mark]:
    """Due Tier 4 hand-off notices (and so, human task steps) with the callback that records delivery."""
    handoff = _sibling("model_garden_notice_handoff", "handoff.py")
    due = handoff.due_notices(root, now=now, escalate_to=escalate_to)

    def mark(item: Mapping[str, Any], via: str) -> Any:
        notice = item["notice"]
        return handoff.mark_notified(root, notice["handoffId"], notice["kind"], via=via, now=now)

    return _normalise("handoff", due, lambda notice: f"{notice['handoffId'][:12]}:{notice['kind']}"), mark


def connection_source(store: Any, health: Mapping[str, Mapping[str, Any]], connectors: Mapping[str, Mapping[str, Any]],
                      grants: Iterable[Mapping[str, Any]] = (), *, now: int, escalate_to: str | None = None,
                      **options: Any) -> tuple[list[dict[str, Any]], Mark]:
    """Due connection and grant notices from a ``NoticeStore`` with the callback that records delivery.

    ``options`` pass through to ``NoticeStore.due`` (for example ``acknowledge_within_seconds``).
    """
    due = store.due(health, connectors, grants, now=now, escalate_to=escalate_to, **options)

    def mark(item: Mapping[str, Any], via: str) -> Any:
        return store.mark_notified(item["notice"]["key"], via=via, now=now)

    return _normalise("connection", due, lambda notice: f"{str(notice['key'])[:12]}:{notice['kind']}"), mark


def _record(audit: Audit | None, action: str, item: Mapping[str, Any], **extra: Any) -> None:
    if audit is not None:
        details = {"source": item["source"], "kind": item["kind"], "reference": item["reference"], **extra}
        try:
            audit(action, "runtime:notice-delivery", details)
        except Exception:  # noqa: BLE001 - a ledger problem must not stop other notices from being delivered
            pass


def deliver(
    sources: Iterable[tuple[list[dict[str, Any]], Mark]],
    directory: Directory,
    senders: Mapping[str, Sender],
    *,
    audit: Audit | None = None,
) -> list[dict[str, Any]]:
    """Send every due notice through its recipient's channel and record each delivery; returns one result per notice."""
    results: list[dict[str, Any]] = []
    for notices, mark in sources:
        for item in notices:
            base = {"source": item["source"], "kind": item["kind"], "reference": item["reference"], "to": item["to"]}
            if item["to"] is None:
                _record(audit, "notice.undeliverable", item, reason="unowned")
                results.append({**base, "status": "unowned", "via": None})
                continue
            route = directory.lookup(item["to"])
            if route is None or (route[0] != "inbox" and route[0] not in senders):
                _record(audit, "notice.undeliverable", item, reason="no-channel", channel=route[0] if route else None)
                results.append({**base, "status": "no-channel", "via": route[0] if route else None})
                continue
            channel, address = route
            if channel != "inbox":
                try:
                    senders[channel](address, item["text"])
                except Exception as exc:  # noqa: BLE001 - one failed send never stops the batch; keep the type only
                    _record(audit, "notice.delivery-failed", item, channel=channel, error=type(exc).__name__)
                    results.append({**base, "status": "failed", "via": channel, "error": type(exc).__name__})
                    continue
            try:
                mark(item, channel)
            except Exception as exc:  # noqa: BLE001 - sent but not recorded: the next run sends again rather than lose it
                _record(audit, "notice.delivery-failed", item, channel=channel, error="unmarked:" + type(exc).__name__)
                results.append({**base, "status": "unmarked", "via": channel, "error": type(exc).__name__})
                continue
            _record(audit, "notice.delivered", item, channel=channel)
            results.append({**base, "status": "delivered", "via": channel})
    return results
