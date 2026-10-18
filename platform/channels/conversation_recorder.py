"""Record finished calls and verified-sender emails into the cross-channel conversation timeline (#115, #108, #109).

``conversations.py`` keeps one ordered timeline per customer and links a conversation to a customer only on verified evidence.
This is the thin recorder between the channels that already produce finished conversations (the call-transcript filing step and
the support mailbox) and that store. It adds **references and labels only**: a transcript or message reference, an outcome label,
a digest of a model-written summary, ledger request ids and a hand-off marker; never a turn, a message body, an address or a
number. It is best-effort by design (a caller wraps it so a recording problem never stops filing or a reply) and the voice path is wired through
``scripts/runtime-file-calls.py`` (``client-operator.py file-calls``), the mail path through the runtime worker.

- **Voice.** A call becomes a conversation keyed on its call hash. Its transcript, outcome, summary digest (only when the record has
  a model summary; an extractive one stays in the transcript store), each ledger request id and a transfer all go on the timeline.
  The caller's number is a transport fact, so it is recorded as *asserted* evidence if the caller passes it at all, and a voice
  conversation therefore never links to a customer by itself: at most it is queued for a person to confirm.
- **Email.** A thread becomes a conversation keyed on a digest of its thread id (Message-IDs contain characters a key must not). The
  message, the handling outcome and the hand-off to a person are recorded. A sender the receiving server verified is *verified*
  evidence; an unverified sender adds no evidence at all.
- **Linking is the store's rule, not this module's.** ``resolve`` is called with a directory built from the Tier 0 contact log
  (``contacts_directory``); a contact "holds" an email or phone as recorded, and only verified evidence can link.
- **Idempotent.** Every identifier is derived from the call or message, so a re-run, a retry or a redelivered email adds nothing.
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,59}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _conversations():
    name = "model_garden_recorder_conversations"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parents[1] / "core" / "conversations.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def contacts_directory(contact_store: Any) -> Callable[[str, str], list[str]]:
    """A ``directory(kind, digest)`` over the Tier 0 contact log: which contacts hold an email or phone with this digest."""
    conversations = _conversations()
    index: dict[tuple[str, str], list[str]] = {}
    for contact_id, kind, value in contact_store.identities():
        try:
            digest = conversations.evidence_digest(kind, value)
        except ValueError:
            continue
        index.setdefault((kind, digest), []).append(contact_id)

    def directory(kind: str, digest: str) -> list[str]:
        return list(index.get((kind, digest), []))

    return directory


def _hex(value: str, length: int = 32) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class ConversationRecorder:
    def __init__(self, store: Any, *, agent: str, directory: Callable[[str, str], list[str]] | None = None, agent_version: str | None = None) -> None:
        self._store, self._agent, self._directory, self._version = store, agent, directory, agent_version

    def _link(self, conversation_id: str, now: int) -> dict[str, Any]:
        if self._directory is None:
            return {"state": "unlinked"}
        return self._store.resolve(conversation_id, self._directory, now=now)

    def record_call(self, record: Mapping[str, Any], *, caller_number: str | None = None, now: int | None = None) -> dict[str, Any]:
        """Put one stored call transcript on the timeline (``record`` is a ``CallTranscript`` document)."""
        call_hash = record.get("callHash")
        started = record.get("timestamp")
        if not isinstance(call_hash, str) or not call_hash or not isinstance(started, int) or isinstance(started, bool):
            return {"state": "skipped"}
        ended = record["endedAt"] if isinstance(record.get("endedAt"), int) and not isinstance(record.get("endedAt"), bool) else started
        conversation = self._store.open("voice", call_hash, agent=self._agent, agent_version=self._version, now=started)
        cid = conversation["id"]
        self._store.add_interaction(cid, kind="call", external_id="call", at=started, ref=f"transcript:{call_hash}", speaker="caller")
        outcome = record.get("outcome")
        if isinstance(outcome, str) and _LABEL.match(outcome):
            self._store.add_interaction(cid, kind="outcome", external_id="outcome", at=ended, ref=f"outcome:{call_hash}", label=outcome)
            if outcome == "transferred":
                self._store.add_interaction(cid, kind="handoff", external_id="handoff", at=ended, ref=f"handoff:{call_hash}", label="transferred")
        summary = record.get("summary")
        if isinstance(summary, str) and summary.strip() and record.get("summarySource") != "extractive":
            source = record.get("summarySource")
            label = source if isinstance(source, str) and _LABEL.match(source) else None
            self._store.add_interaction(cid, kind="summary", external_id="summary", at=ended, ref=f"summary:{call_hash}", label=label,
                                        content_digest=hashlib.sha256(summary.strip().encode("utf-8")).hexdigest())
        for request_id in (record.get("requestIds") or [])[:50]:
            if isinstance(request_id, str) and _DIGEST.match(request_id):
                self._store.add_interaction(cid, kind="action", external_id=request_id[:32], at=ended, ref=f"action:{request_id[:24]}", request_id=request_id)
        if caller_number:
            try:  # a transport fact, never proof of who is calling
                self._store.add_evidence(cid, "phone", caller_number, verified=False, provenance="caller-id")
            except ValueError:
                pass
        return {"state": "recorded", "conversationId": cid, "link": self._link(cid, now if now is not None else ended)["state"]}

    def record_email(
        self, thread_id: str, message_id: str, received_at: int, *, verified_sender: str | None = None, outcome: str | None = None, handed_to_person: bool = False,
    ) -> dict[str, Any]:
        """Put one handled inbound email on its thread's conversation."""
        thread_key, message_key = _hex(thread_id), _hex(message_id)
        conversation = self._store.open("email", thread_key, agent=self._agent, agent_version=self._version, now=received_at)
        cid = conversation["id"]
        self._store.add_interaction(cid, kind="message", external_id=f"m-{message_key}", at=received_at, ref=f"message:{message_key}", speaker="customer")
        if isinstance(outcome, str) and _LABEL.match(outcome):
            self._store.add_interaction(cid, kind="outcome", external_id=f"o-{message_key}", at=received_at, ref=f"outcome:{message_key}", label=outcome)
        if handed_to_person:
            self._store.add_interaction(cid, kind="handoff", external_id=f"h-{message_key}", at=received_at, ref=f"handoff:{message_key}", label="needs-person")
        if verified_sender:
            self._store.add_evidence(cid, "email", verified_sender, verified=True, provenance="email-dkim")
        return {"state": "recorded", "conversationId": cid, "link": self._link(cid, received_at)["state"]}
