"""One inbound path for every text channel: verify, de-duplicate, route, answer (#82, #83, #84, #88, #97, #99).

The Slack, Teams and web chat adapters decide *who is speaking* and the hand-off, schedule and approval modules decide what a
message may do. This is the small router between them and an HTTP server (``platform/runtime/gateway.py``). It opens no socket,
holds no credential and sends nothing itself: it is handed the raw request and the adapters, and returns an HTTP-shaped
``Response`` plus the replies the adapters sent.

- **Verification first.** A request is verified by its channel adapter before anything in it is read. A failed verification is a
  401 that says nothing about why. A request the adapter deliberately ignores (a bot's own message, an edit, channel chatter) is a
  200 with the named reason, so the provider does not retry it.
- **Authority from the channel.** The principal is the verified sender mapped through the ``PrincipalDirectory``. Nothing in the
  text changes it. An unmapped sender is a public, read-only principal.
- **Routing in a fixed order.** A hand-off reply (``done <ref>``) goes to ``HandoffReplies`` and closes the hand-off under the
  channel identity; a schedule command (``schedule create ...``) goes to ``schedule_commands`` for a mapped person only; ``inbox``
  shows what is waiting (see ``platform/core/inbox.py``) to a mapped person in a direct conversation only; ``assign <agent>: <goal>``
  hands a task to a team Agent under the speaker's own identity (a mapped person in a direct conversation only; the injected
  ``assign`` callable applies the delegation rules and budget, and the ledger notes who assigned to whom, never the goal); anything
  else is a question for the coworker, answered by the injected ``respond`` callable. Hand-off replies are accepted only in a
  direct conversation.
- **Approvals.** A verified button click goes to ``decide_from_channel``; the responder must be a mapped approver. The reply to
  the click states what was recorded and never more.
- **Retries are harmless.** Slack and Teams redeliver; a message already handled (same channel, conversation and message id) is
  acknowledged without running again. The set is bounded and kept in memory: after a restart a redelivery of an old message could
  run once more, which is why hand-off closing and approvals are themselves single use.
- **Fast acknowledgement.** With an ``executor`` the work runs after the 200 is returned, because Slack gives a handler three
  seconds. Without one the work is done before returning (web chat, tests).

It is **not a live integration**: no Slack app, Teams bot or web page has been exercised against the real providers.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_SEEN = 5000
MAX_REPLY_CHARS = 3500
MAX_ATTACHMENTS = 3
INBOX_COMMAND = re.compile(r"^/?inbox$", re.IGNORECASE)
PAUSE_COMMAND = re.compile(r"^/?(pause|resume)\s+(?:the\s+)?coworker\b[\s:.-]*(.*)$", re.IGNORECASE | re.DOTALL)
ASSIGN_COMMAND = re.compile(r"^/?(?:assign|delegate)\s+(?:to\s+)?([a-z0-9][a-z0-9-]{0,62})\s*(?::|\s-\s)\s*(.+)$", re.IGNORECASE | re.DOTALL)
Audit = Callable[[str, str, dict[str, Any]], Any]


def parse_assign(words: str) -> tuple[str, str] | None:
    """``(agent, goal)`` from ``assign <agent>: <goal>`` (or ``delegate``), else None."""
    found = ASSIGN_COMMAND.match(words.strip())
    if found is None or not found.group(2).strip():
        return None
    return found.group(1).lower(), found.group(2).strip()


def _sibling(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parent / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


TEXT = _sibling("model_garden_text_channel", "text_channel.py")
HANDOFF_REPLY = _sibling("model_garden_ingress_handoff_reply", "../core/handoff_reply.py")
SCHEDULE_COMMANDS = _sibling("model_garden_ingress_schedule_commands", "../core/schedule_commands.py")


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)


def _json(status: int, document: Mapping[str, Any]) -> Response:
    return Response(status, json.dumps(document, sort_keys=True).encode("utf-8"))


class ChannelIngress:
    def __init__(
        self,
        adapters: Mapping[str, Any],
        directory: Any,
        *,
        respond: Callable[[Any, Any], str | None],
        decide: Callable[..., dict[str, Any]],
        handoff_replies: Any | None = None,
        schedules: Any | None = None,
        agent: str = "coworker",
        executor: Any | None = None,
        audit: Audit | None = None,
        deliverables: Callable[[Any, Any, str], Any] | None = None,
        inbox: Callable[[Any, Any], str] | None = None,
        assign: Callable[[str, str, str], str] | None = None,
        observed: Callable[[Any], None] | None = None,
        pause: Callable[[str, str, str], str] | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._inbox = inbox
        self._pause = pause  # (admin, "pause" | "resume", reason) -> reply; only a mapped admin in a direct conversation reaches it
        self._assign = assign  # (person, agent, goal) -> reply; raises ValueError with a safe message when refused
        self._observed = observed  # told once about each fresh conversational message (not a command or a redelivery), e.g. to raise an event
        self._deliverables = deliverables
        self._directory = directory
        self._respond = respond
        self._decide = decide
        self._replies = handoff_replies
        self._schedules = schedules
        self._agent = agent
        self._executor = executor
        self._audit = audit
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._lock = threading.Lock()

    def _note(self, action: str, who: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            try:
                self._audit(action, who, details)
            except Exception:  # noqa: BLE001 - the ledger must not turn a handled message into a crash
                pass

    def _first_time(self, message: Any) -> bool:
        key = (message.channel, message.conversation_id, message.message_id)
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > MAX_SEEN:
                self._seen.popitem(last=False)
        return True

    @staticmethod
    def _is_direct(message: Any) -> bool:
        if message.channel == "slack":
            return message.conversation_id.startswith("D")
        if message.channel == "teams":
            return message.thread_id is None  # the Teams adapter threads only channel and group conversations
        return True  # web chat is one signed-in person

    # -- messages ------------------------------------------------------------------------------------

    def message(self, channel: str, body: bytes, headers: Mapping[str, str]) -> Response:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return _json(404, {"error": "unknown channel"})
        challenge = getattr(adapter, "handle_challenge", None)
        try:
            if callable(challenge):
                echoed = challenge(body, headers)
                if echoed is not None:
                    return Response(200, echoed.encode("utf-8"), "text/plain")
            inbound = adapter.parse_inbound(body, headers)
        except TEXT.ChannelVerificationError:
            return _json(401, {"error": "not verified"})
        except Exception as exc:  # noqa: BLE001 - adapters raise their own ``*Ignored`` for traffic they skip on purpose
            if type(exc).__name__.endswith("Ignored"):
                return _json(200, {"ignored": str(exc)[:60] or "ignored"})
            raise
        if not self._first_time(inbound):
            return _json(200, {"duplicate": True})
        if self._executor is not None:
            self._executor.submit(self._process, adapter, inbound)
            return _json(200, {"accepted": True})
        return _json(200, {"status": self._process(adapter, inbound)})

    def _send(self, adapter: Any, inbound: Any, text: str, attachments: tuple[Any, ...] = ()) -> None:
        adapter.send_reply(TEXT.OutboundReply(
            conversation_id=inbound.conversation_id, thread_id=inbound.thread_id, text=text[:MAX_REPLY_CHARS], attachments=attachments,
        ))

    def _attachments(self, inbound: Any, principal: Any, answer: str) -> tuple[Any, ...]:
        """Deliverables the reply should carry, from the injected policy; a problem here never stops the answer."""
        if self._deliverables is None:
            return ()
        try:
            return tuple(self._deliverables(inbound, principal, answer))[:MAX_ATTACHMENTS]
        except Exception as exc:  # noqa: BLE001 - the answer itself still goes out
            self._note("channel.deliverables-failed", "runtime:gateway", {"channel": inbound.channel, "error": type(exc).__name__})
            return ()

    def _process(self, adapter: Any, inbound: Any) -> str:
        try:
            principal = self._directory.principal_for(inbound)
            text = inbound.text
            if self._replies is not None and HANDOFF_REPLY.parse(text) is not None:
                result = self._replies.handle(
                    inbound.channel, inbound.conversation_id, text, direct=self._is_direct(inbound),
                    sender_id=inbound.sender.channel_user_id,
                )
                self._send(adapter, inbound, result["reply"])
                return f"handoff:{result['status']}"
            if self._schedules is not None and SCHEDULE_COMMANDS.is_command(text):
                reply = SCHEDULE_COMMANDS.handle(
                    self._schedules, text, principal=principal.identity if principal.mapped else None,
                    agent=self._agent, admin="admin" in principal.roles,
                )
                self._send(adapter, inbound, reply or "")
                return "schedule-command"
            pause_command = PAUSE_COMMAND.match(text.strip()) if self._pause is not None else None
            if pause_command is not None:
                if principal.mapped and "admin" in principal.roles and self._is_direct(inbound):
                    verb = pause_command.group(1).lower()
                    reply = self._pause(principal.identity, verb, pause_command.group(2).strip())
                    self._note(f"channel.coworker-{verb}", principal.identity, {"channel": inbound.channel})
                else:  # the same words for everyone else, in any channel: nothing is revealed and nothing changes
                    reply = "Only an administrator can pause or resume me, in a direct message."
                self._send(adapter, inbound, reply)
                return "pause-command"
            if self._inbox is not None and INBOX_COMMAND.match(text.strip()):
                if principal.mapped and self._is_direct(inbound):
                    reply = self._inbox(inbound, principal)
                    self._note("channel.inbox-viewed", principal.identity, {"channel": inbound.channel})
                else:  # the same words for a stranger and for a shared channel: nothing is revealed
                    reply = "I can show what is waiting only to a team member in a direct message."
                self._send(adapter, inbound, reply)
                return "inbox-command"
            parsed = parse_assign(text) if self._assign is not None else None
            if parsed is not None:
                agent, goal = parsed
                if principal.mapped and self._is_direct(inbound):
                    try:
                        reply = self._assign(principal.identity, agent, goal)
                        self._note("channel.task-assigned", principal.identity, {"channel": inbound.channel, "agent": agent, "goalLength": len(goal)})
                    except ValueError as exc:  # a refusal names the rule, never the goal
                        reply = f"I could not hand that over: {exc}. Nothing was assigned."
                else:  # the same words for a stranger and for a shared channel
                    reply = "I can hand a task to an Agent only for a team member in a direct message."
                self._send(adapter, inbound, reply)
                return "assign-command"
            if self._observed is not None:
                try:
                    self._observed(inbound)
                except Exception as exc:  # noqa: BLE001 - announcing the message must never stop it being answered
                    self._note("channel.event-failed", "runtime:gateway", {"channel": inbound.channel, "error": type(exc).__name__})
            answer = self._respond(inbound, principal)
            if answer:
                self._send(adapter, inbound, answer, self._attachments(inbound, principal, answer))
                return "answered"
            return "no-answer"
        except Exception as exc:  # noqa: BLE001 - say that it failed, never what the message held
            self._note("channel.message-failed", "runtime:gateway", {"channel": inbound.channel, "error": type(exc).__name__})
            try:
                self._send(adapter, inbound, "Sorry, I could not handle that. Nothing was changed.")
            except Exception:  # noqa: BLE001
                pass
            return "failed"

    # -- approvals -----------------------------------------------------------------------------------

    def approval(self, channel: str, body: bytes, headers: Mapping[str, str]) -> Response:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return _json(404, {"error": "unknown channel"})
        try:
            response = adapter.parse_approval_response(body, headers)
        except TEXT.ChannelVerificationError:
            return _json(401, {"error": "not verified"})
        if response is None:
            return _json(200, {"ignored": "not-an-approval"})
        try:
            decision = TEXT.decide_from_channel(response, self._directory, self._decide)
        except PermissionError:
            self._note("channel.approval-refused", "runtime:gateway", {"channel": channel, "request": response.request_id[:16]})
            return _json(403, {"error": "not an approver", "reply": "Only a mapped approver can decide this request."})
        except (ValueError, OSError):
            return _json(409, {"error": "no pending request", "reply": "That request is not waiting for a decision."})
        verb = "approved" if decision.get("approved") else "rejected"
        return _json(200, {"recorded": verb, "requestId": response.request_id, "reply": f"Recorded: {verb} by you."})
