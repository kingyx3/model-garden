"""Let the named person answer a hand-off, or a human task step, from their own channel (#88, #97).

The hand-off store, the notices and the delivery runner already get a request to a person and record that it arrived.
What was missing is the way back: the person says ``done`` or ``declined`` where they were told, and that answer
closes the record under *their* identity instead of an identity an operator typed. This module is that return path. It
holds no credential, imports no provider and sends nothing; the channel adapter hands it a message the adapter has
already authenticated, and the caller sends back the one-line ``reply`` it returns. It is **not wired into the sidecar
or a channel adapter**, and no real channel reply has had live proof.

A reply is one line: ``done <ref>``, ``declined <ref> [note]`` or ``seen <ref>`` where ``<ref>`` is the reference printed
in the notice (at least 8 characters of the hand-off id).

- **Identity comes from the channel, never from the words.** The sender is whoever the people directory
  (:class:`notice_delivery.Directory`) lists for exactly this channel and conversation, and the conversation must be a
  direct one: in a shared channel anyone could type the reference, so it is refused. A person whose directory entry
  records their channel ``identity`` must also match it. Nothing in the message text names or changes who is answering.
- **Only the right person.** The hand-off's assignee may answer; the named escalation contact may answer once it is
  overdue. Anyone else, an unknown reference and a reference that matches more than one hand-off all get the same neutral
  reply, so a sender cannot probe which hand-offs exist. Refusals are ledgered without the message text.
- **seen is not done.** ``seen`` records an acknowledgement and only quiets the pre-deadline reminder; it never closes
  the hand-off and never stops the overdue escalation.
- **Truthful and single use.** Closing goes through the injected ``complete`` callback (the governed
  ``complete_handoff`` ledgers it as a tier 4 action naming the person and the channel). A second answer to a closed
  hand-off changes nothing and is told what was recorded. The reply states what happened and never claims more.
- **The note is data.** Whatever the person writes after the outcome is stored as the note (bounded, one line) and for a
  human task step returned to the task as input; it never edits authority. The ledger records the reference, channel,
  outcome and status, never the text.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

MAX_NOTE = 500
MIN_REFERENCE = 8
STATUSES = ("closed", "acknowledged", "not-a-reply", "unknown-sender", "not-direct", "no-match", "not-assignee", "already-closed")
_REPLY = re.compile(r"^\s*(done|declined?|seen|ack(?:nowledged)?)[\s:]+([0-9a-f]{8,64})\b[\s:,-]*(.*)$", re.IGNORECASE | re.DOTALL)
_NEUTRAL = "I could not match that to an open hand-off assigned to you. Use the reference from the notice."
_UNCLEAR = "To answer, reply with: done <reference>, declined <reference> [note], or seen <reference>."
Complete = Callable[..., Any]
Audit = Callable[[str, str, dict[str, Any]], Any]


class ReplyError(ValueError):
    """Raised when the handler is set up wrongly; a bad reply is a normal result, not an exception."""


def _handoff():
    name = "model_garden_reply_handoff"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("handoff.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def parse(text: Any) -> dict[str, str] | None:
    """``{"outcome": done|declined|seen, "reference": hex, "note": str}`` for a well-formed reply, else ``None``."""
    if not isinstance(text, str) or len(text) > 4000:
        return None
    match = _REPLY.match(text.strip())
    if not match:
        return None
    word = match.group(1).lower()
    outcome = "done" if word == "done" else ("seen" if word.startswith(("seen", "ack")) else "declined")
    note = " ".join(match.group(3).split())[:MAX_NOTE]
    return {"outcome": outcome, "reference": match.group(2).lower(), "note": note}


class HandoffReplies:
    """Turns an authenticated inbound reply into an acknowledgement or a closed hand-off."""

    def __init__(
        self,
        root: pathlib.Path,
        directory: Any,
        *,
        complete: Complete,
        escalate_to: str | None = None,
        audit: Audit | None = None,
    ) -> None:
        if not callable(complete):
            raise ReplyError("complete must be a callable that closes a hand-off (for example governed complete_handoff)")
        if not hasattr(directory, "identify"):
            raise ReplyError("directory must be a notice_delivery.Directory")
        self._root = pathlib.Path(root)
        self._directory = directory
        self._complete = complete
        self._escalate_to = escalate_to
        self._audit = audit

    def _ledger(self, person: str, status: str, reference: str, channel: str, outcome: str | None) -> None:
        if self._audit is not None:
            self._audit(
                "handoff.reply", person,
                {"status": status, "reference": reference[:12], "channel": channel[:32], **({"outcome": outcome} if outcome else {})},
            )

    def _resolve(self, reference: str) -> tuple[dict[str, Any] | None, str]:
        handoff = _handoff()
        open_ones = [item for item in handoff.pending(self._root) if item["id"].startswith(reference)]
        if len(open_ones) == 1:
            return open_ones[0], "open"
        if len(open_ones) > 1:
            return None, "ambiguous"
        done = [item for item in handoff.closed(self._root) if item.get("id", "").startswith(reference)]
        if done:
            return sorted(done, key=lambda item: item.get("completedAt", 0))[-1], "closed"
        return None, "none"

    def handle(
        self,
        channel: str,
        conversation_id: str,
        text: str,
        *,
        direct: bool,
        sender_id: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Apply one authenticated inbound message. Returns ``status``, the ``reply`` to send back and the hand-off ``id`` if any.

        ``channel`` and ``conversation_id`` must come from the adapter's verified inbound message, ``direct`` is true only
        for a one-to-one conversation, and ``sender_id`` is the adapter-verified user id on that channel.
        """
        parsed = parse(text)
        if parsed is None:
            return {"status": "not-a-reply", "reply": _UNCLEAR, "id": None}
        if direct is not True:
            self._ledger("unknown", "not-direct", parsed["reference"], channel, None)
            return {"status": "not-direct", "reply": "Please answer in a direct message so I know it is you.", "id": None}
        person = self._directory.identify(channel, conversation_id, sender_id)
        if person is None:
            self._ledger("unknown", "unknown-sender", parsed["reference"], channel, None)
            return {"status": "unknown-sender", "reply": _NEUTRAL, "id": None}
        document, state = self._resolve(parsed["reference"])
        if document is None:
            self._ledger(person, "no-match", parsed["reference"], channel, None)
            return {"status": "no-match", "reply": _NEUTRAL, "id": None}
        handoff = _handoff()
        overdue = state == "open" and (int(time.time()) if now is None else now) > int(document["deadline"])
        allowed = person == document.get("assignee") or (
            overdue and self._escalate_to is not None and person == self._escalate_to
        )
        if not allowed:
            self._ledger(person, "not-assignee", parsed["reference"], channel, None)
            return {"status": "not-assignee", "reply": _NEUTRAL, "id": None}
        if state == "closed":
            self._ledger(person, "already-closed", parsed["reference"], channel, None)
            return {"status": "already-closed", "reply": handoff.describe(document, now=now)["message"], "id": document["id"]}
        reference = document["id"][:12]
        if parsed["outcome"] == "seen":
            handoff.acknowledge(self._root, document["id"], person, via=channel, now=now)
            self._ledger(person, "acknowledged", reference, channel, "seen")
            return {
                "status": "acknowledged",
                "reply": f"Noted, thank you. {document['tool']} is still waiting: reply done {reference} or declined {reference} when it is dealt with.",
                "id": document["id"],
            }
        outcome = "done" if parsed["outcome"] == "done" else "declined"
        closed = self._complete(document["id"], outcome, person, note=parsed["note"] or None, via=channel)
        self._ledger(person, "closed", reference, channel, outcome)
        verb = "that you carried out" if outcome == "done" else "that you declined"
        return {
            "status": "closed",
            "reply": f"Recorded {verb} {closed.get('tool', document['tool'])}. Reference {reference}.",
            "id": document["id"],
        }
