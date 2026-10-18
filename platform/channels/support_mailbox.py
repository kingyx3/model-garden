"""The customer support mailbox: the same coworker answers inbound email, draft-first, as a public principal.

This is the credential-free handler for one inbound message to a support or enquiries address (#109). It joins parts that
already exist: the email surface (sender verification, loop protection, threading; ``email_channel.py``), the portable
``mail.draft`` Tool (Gmail or Outlook adapters, or any executor with the same shape) and the CRM sync ledger
(``crm_filing.py``). It reads no mailbox, holds no credential and calls no model; the classifier and the answerer are injected.
The runtime worker (``platform/runtime/worker.py``) feeds it from the built-in spool with the coworker as classifier and answerer
(answers only from a governed ``kb.search``) and a Tier 0 draft store as ``mail.draft``.

- **The sender is a public principal and the body is data.** Authority comes only from the receiving server's verified
  ``Authentication-Results`` (``email_channel.py``); this handler has no approval path at all and passes the message text only to
  the injected classifier and answerer, never to a decision. Nothing in an email can change policy, approve an action or choose
  who the reply goes to.
- **Loop protection first.** An auto-reply, bounce, list mail, no-reply or mailer sender or our own message is skipped before
  anything else happens (nothing is drafted or filed). The per-thread reply limit counts only replies that are really drafted,
  and a thread that has reached it goes to a person rather than going quiet.
- **Fixed categories, people for the rest.** ``enquiry`` and ``booking`` can be answered; ``spam`` is dropped; ``complaint``,
  ``other`` and anything the classifier returns that is not one of the categories (or that raises) go to a person. No reply is
  drafted for them.
- **Approved knowledge only.** The answerer must return text *and* the identifiers of the approved sources it used; no sources
  (or no answer) means a person handles it, so the coworker never answers from nothing.
- **Draft-first.** The only mail Tool this handler calls is ``mail.draft`` and only to the *verified* sender address; a sender
  whose address the receiving server did not verify gets no draft (a forged ``From`` could point the reply at a third party).
  Sending is a separate step: by default nothing is requested and a person (or the governed ``mail.send``, which needs
  approval under the client's policy) sends the draft. A client may list ``enquiry`` and/or ``booking`` in ``send_categories`` so
  the handler *requests* ``mail.send`` for that draft through an injected governed executor; whether it runs is still decided by
  the approval policy, and ``complaint``, ``other`` and ``spam`` can never be listed.
- **A person's thread is a ticket too.** When a ticket opener is supplied (the worker supplies the Tier 0 ticket store when it is bound),
  every thread handed to a person also opens one ticket carrying the thread reference, category and reason only, never the message
  text, so it stays in the runtime inbox until someone finishes it; the same thread and reason always give the same ticket and a
  failure to open one never loses the thread.
- **Filed through the sync ledger.** Each handled thread (including those handed to a person) is filed as one email interaction,
  one ledger entry per thread, with the thread's digest as its reference and no message text. A CRM outage never stops the
  handler; the entry waits in the ledger.

Not done here: reading the mailbox, the model classifier and answerer, serving ``mail.*`` from the sidecar, owner OAuth consent
and the coworker's own mailbox grant (#129, #110), and live proof. ``mail.draft`` takes only recipients, subject and body, so the
draft carries a ``Re:`` subject but not ``In-Reply-To`` or ``Auto-Submitted`` headers; a transport that sends the draft must add
those (``EmailChannelAdapter.reply_headers``).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

CATEGORIES = ("enquiry", "booking", "complaint", "spam", "other")
ANSWERABLE = ("enquiry", "booking")
SENDABLE = ANSWERABLE
MAX_BODY = 50_000
MAX_SOURCES = 20
IDENTITY = "runtime:support-mailbox"
Audit = Callable[[str, str, dict[str, Any]], Any]


def _module(name: str, relative: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).parent / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _email():
    return _module("model_garden_support_email", "email_channel.py")


def _filing():
    return _module("model_garden_support_filing", "../core/crm_filing.py")


def thread_reference(thread_id: str) -> str:
    """A stable, non-identifying reference for a thread (the digest of its root Message-ID)."""
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]


def _iso(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace("+00:00", "Z")


class MessageLog:
    """Which inbound messages were already handled, so a redelivered message never gets a second draft.

    Holds digests of Message-IDs only, in one private file under a lock. A message is recorded *after* it was handled, so a
    crash in between can produce a second draft (visible to a person, never sent) but can never lose a customer's message.
    """

    MAX_ENTRIES = 20000

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / "support-mailbox-handled.json"

    def _digest(self, message_id: str) -> str:
        return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]

    def _read(self) -> list[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def seen(self, message_id: str) -> bool:
        return self._digest(message_id) in self._read()

    def mark(self, message_id: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.root / ".support-mailbox.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            handled = self._read()
            digest = self._digest(message_id)
            if digest in handled:
                return
            handled = [*handled, digest][-self.MAX_ENTRIES:]
            temporary = self.path.with_suffix(".tmp")
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(handled))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


class SupportMailbox:
    def __init__(
        self,
        adapter: Any,
        directory: Any,
        mail: Mapping[str, Callable[[dict[str, Any]], Mapping[str, Any]]],
        *,
        classify: Callable[[Any], str],
        answer: Callable[[Any, str], Mapping[str, Any] | None],
        ledger: Any | None = None,
        crm: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
        contact_for: Callable[[str], str] | None = None,
        request_send: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
        send_categories: Iterable[str] = (),
        escalate: Callable[[dict[str, Any]], Any] | None = None,
        ticket: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
        handled: MessageLog | None = None,
        audit: Audit | None = None,
        conversations: Any | None = None,
        payloads: Any | None = None,
    ) -> None:
        if not callable(mail.get("mail.draft")):
            raise ValueError("the support mailbox needs a mail.draft executor")
        chosen = tuple(dict.fromkeys(send_categories))
        refused = [item for item in chosen if item not in SENDABLE]
        if refused:
            raise ValueError(
                "only enquiry and booking replies can be offered for sending without a person; "
                + ", ".join(refused) + " always go to a person"
            )
        if chosen and request_send is None:
            raise ValueError("send_categories needs the governed mail.send executor to request sending through")
        if (ledger is None) != (crm is None):
            raise ValueError("filing needs both the sync ledger and a CRM executor")
        self._adapter, self._directory, self._draft = adapter, directory, mail["mail.draft"]
        self._classify, self._answer = classify, answer
        self._ledger, self._crm = ledger, crm
        self._payloads = payloads  # a crm_filing.PayloadStore: the filing arguments are kept until the CRM acknowledges, so a retry can send them
        self._contact_for = contact_for or (lambda _address: "unmatched")
        self._request_send, self._send_categories = request_send, frozenset(chosen)
        self._escalate, self._handled, self._audit = escalate, handled, audit
        self._ticket = ticket  # opens a Tier 0 ticket for a thread a person must handle; it is handed identifiers, never message text
        self._conversations = conversations  # a ConversationRecorder: the thread is also put on the cross-channel timeline

    # -- helpers -----------------------------------------------------------------------------

    def _category(self, message: Any) -> str:
        try:
            label = self._classify(message)
        except Exception:  # noqa: BLE001 - a failing classifier is an unknown category, which goes to a person
            return "unknown"
        label = label.strip().lower() if isinstance(label, str) else ""
        return label if label in CATEGORIES else "unknown"

    def _answer_for(self, message: Any, category: str) -> tuple[str, list[str]] | None:
        try:
            found = self._answer(message, category)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(found, Mapping):
            return None
        text, sources = found.get("text"), found.get("sources")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_BODY:
            return None
        if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= MAX_SOURCES or not all(
            isinstance(item, str) and item for item in sources
        ):
            return None
        return text, list(sources)

    def _file(self, address: str | None, thread: str, category: str, status: str, now: int) -> dict[str, Any] | None:
        if self._ledger is None or self._crm is None:
            return None
        reference = thread_reference(thread)
        arguments = {
            "objectId": self._contact_for(address) if address else "unmatched",
            "interactionType": "email", "channel": "email", "direction": "inbound", "timestamp": _iso(now),
            "summary": f"Support email thread handled by the coworker: category {category}, {status}.",
            "outcome": status, "transcriptRef": f"email:{reference}",
        }
        try:
            outcome = _filing().file_interaction(
                self._ledger, self._crm, arguments, channel="email", external_id=f"thread-{reference}",
                interaction_type="email", now=now, store=self._payloads,
            )
        except Exception as exc:  # noqa: BLE001 - filing must never stop the handler
            return {"state": "unavailable", "errorType": type(exc).__name__}
        return {"state": outcome["state"], "attempted": outcome["attempted"], "duplicate": outcome["duplicate"], "providerRef": outcome["providerRef"]}

    def _record(self, result: dict[str, Any]) -> dict[str, Any]:
        if self._audit is not None:
            self._audit("support-mailbox.handled", IDENTITY, {
                key: result[key] for key in ("threadRef", "status", "reason", "category", "draftId", "send") if key in result
            })
        return result

    # -- handling ----------------------------------------------------------------------------

    def handle(self, raw: bytes, *, now: int) -> dict[str, Any]:
        """Handle one inbound message. Never raises for message content; returns where the thread stands."""
        email = _email()
        try:
            message = self._adapter.parse_inbound(raw)
        except email.ChannelVerificationError:
            return {"status": "ignored", "reason": "unreadable"}
        thread = message.thread_id or message.conversation_id
        # messageRef is a short digest of the message id: enough to say "this message" to a trigger or a ledger, never the id itself
        result: dict[str, Any] = {"threadRef": thread_reference(thread), "messageRef": hashlib.sha256(message.message_id.encode("utf-8")).hexdigest()[:16]}
        principal = self._directory.principal_for(message)
        result["principalAccess"] = principal.access  # recorded for the ledger; it never widens what this handler does

        reason = self._adapter.loop_reason(raw)
        if reason is not None:
            return self._record({**result, "status": "ignored", "reason": reason})
        if self._handled is not None and self._handled.seen(message.message_id):
            return self._record({**result, "status": "ignored", "reason": "already-handled"})

        category = self._category(message)
        result["category"] = category
        address = message.sender.channel_user_id
        verified = not address.startswith(email.UNVERIFIED_PREFIX)

        def done(out: dict[str, Any]) -> dict[str, Any]:
            if self._conversations is not None and out.get("status") != "ignored":
                try:  # best-effort: a timeline problem must never stop a reply or lose the message
                    out["conversation"] = self._conversations.record_email(
                        thread, message.message_id, now, verified_sender=address if verified else None, outcome=out.get("status"),
                        handed_to_person=out.get("status") == "needs-person",
                    )["state"]
                except Exception as exc:  # noqa: BLE001
                    out["conversation"] = f"unavailable:{type(exc).__name__}"
            if self._handled is not None:
                self._handled.mark(message.message_id)
            return self._record(out)

        def to_person(why: str) -> dict[str, Any]:
            out = {**result, "status": "needs-person", "reason": why}
            if self._escalate is not None:
                try:
                    self._escalate({"threadRef": out["threadRef"], "category": category, "reason": why})
                except Exception:  # noqa: BLE001 - a failed notification must not lose the thread
                    out["escalation"] = "failed"
            if self._ticket is not None:
                try:  # a thread for a person is also a durable work item; redelivery returns the same ticket
                    opened = self._ticket({"threadRef": out["threadRef"], "category": category, "reason": why})
                    out["ticket"] = opened.get("ticketId") if isinstance(opened, Mapping) else None
                except Exception as exc:  # noqa: BLE001 - a failed ticket must not lose the thread
                    out["ticket"] = f"failed:{type(exc).__name__}"
            out["crm"] = self._file(address if verified else None, thread, category, "needs-person", now)
            return done(out)

        if category == "spam":
            return done({**result, "status": "ignored", "reason": "spam"})
        if category not in ANSWERABLE:
            return to_person("unknown-category" if category == "unknown" else f"category:{category}")
        if not verified:
            return to_person("sender-unverified")
        answered = self._answer_for(message, category)
        if answered is None:
            return to_person("no-approved-answer")
        body, sources = answered
        if not self._adapter.consume_reply(raw):  # counted only for a reply that will really be drafted
            return to_person("thread-reply-limit")
        try:
            draft = self._draft({"to": [address], "subject": self._adapter.reply_headers(raw)["Subject"], "body": body})
            draft_id = draft.get("draftId")
            if not isinstance(draft_id, str) or not draft_id:
                raise ValueError("the mail provider returned no draft reference")
        except Exception as exc:  # noqa: BLE001 - nothing is sent when the draft cannot be made
            return to_person(f"draft-failed:{type(exc).__name__}")
        out = {**result, "status": "drafted", "draftId": draft_id, "sources": len(sources), "send": "approval-required"}
        if category in self._send_categories and self._request_send is not None:
            try:
                sent = self._request_send({"draftId": draft_id})
                state = sent.get("status") if isinstance(sent, Mapping) else None
                out["send"] = {"executed": "sent", "pending-approval": "pending-approval"}.get(state, "not-sent")
            except Exception as exc:  # noqa: BLE001
                out["send"] = "not-sent"
                out["sendError"] = type(exc).__name__
        out["crm"] = self._file(address, thread, category, "reply-drafted", now)
        return done(out)
