"""A task step handed to a named person, durable and resumable, built on the Tier 4 hand-off store.

Judgement calls stay with people (#97): when a task step needs a person, the task does not guess and does not
spin. It opens a hand-off for the named person carrying the exact ask, then reads the answer back later. This
module is the contract for that step; it adds no storage of its own. The pending hand-off record is the durable
pause, so a restart loses nothing, and the existing inbox, notices and ``runtime-handoff.py complete`` are how the
person is told and answers. It is **not wired into a runtime** (there is no multi-Agent task runner yet, #95) and
no channel delivery beyond the built-in inbox is included.

- **The step carries the ask.** The hand-off request holds the task and step identifiers, the exact ask and a
  short context, so the person sees what is needed. Notices still carry only the Tool name and a short reference.
- **Durable pause, three ways out.** ``resolution`` reads the hand-off: still waiting, answered ``done`` (resume
  with the person's note), ``declined``, or timed out. A timeout never resumes silently: it resolves to the
  fallback the task configured (``fail``, ``skip`` or ``escalate`` to another named person).
- **A human cannot widen authority.** The answer is data returned to the task. It never edits the envelope's
  authority, so the step cannot become a way to obtain Tools the task was not allowed.
- **Identity and input are ledgered** as digests: who opened it, who answered, the outcome, whether it was late,
  and a digest of the note, never the note.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
import time
from collections.abc import Mapping
from typing import Any

TOOL = "task.human-step"
FALLBACKS = ("fail", "skip", "escalate")
MAX_ASK = 500
MAX_CONTEXT = 1000


class HumanStepError(ValueError):
    """Raised for an invalid step or one that does not exist."""


def _handoff():
    name = "model_garden_human_step_handoff"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("handoff.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def step_id_for(task_id: str, step: str) -> str:
    """The stable hand-off id for one step of one task: reopening the same step never creates a second."""
    if not isinstance(task_id, str) or not task_id or not isinstance(step, str) or not step:
        raise HumanStepError("a step needs a task id and a step name")
    return hashlib.sha256(json.dumps([task_id, step], separators=(",", ":")).encode("utf-8")).hexdigest()


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise HumanStepError(f"{label} must be a non-empty string of at most {limit} characters")
    return value.strip()


def open_step(
    root: pathlib.Path,
    envelope: Mapping[str, Any],
    *,
    step: str,
    assignee: str,
    ask: str,
    context: str = "",
    timeout_hours: int = 24,
    fallback: str = "fail",
    escalate_to: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Pause the task on ``step`` by opening a hand-off for ``assignee``; reopening returns the same hand-off."""
    if fallback not in FALLBACKS:
        raise HumanStepError(f"fallback must be one of {', '.join(FALLBACKS)}")
    if fallback == "escalate" and not (isinstance(escalate_to, str) and escalate_to.strip()):
        raise HumanStepError("an escalating fallback needs a named person to escalate to")
    task_id = envelope.get("taskId")
    request = {
        "requestId": step_id_for(task_id, step),
        "tool": TOOL,
        "taskId": task_id,
        "step": _text(step, "step", 100),
        "agent": envelope.get("agent"),
        "originatingPrincipal": envelope.get("originatingPrincipal"),
        "ask": _text(ask, "ask", MAX_ASK),
        "context": context.strip()[:MAX_CONTEXT] if isinstance(context, str) else "",
        "fallback": fallback,
        **({"escalateTo": escalate_to.strip()} if fallback == "escalate" else {}),
    }
    try:
        return _handoff().create(
            root, request, capability=None, reason="human-only", assignee=assignee, deadline_hours=timeout_hours, now=now
        )
    except ValueError as exc:
        raise HumanStepError(str(exc)) from exc


def resolution(root: pathlib.Path, envelope: Mapping[str, Any], step: str, *, now: int | None = None) -> dict[str, Any]:
    """Where the step stands: ``waiting``, ``resumed`` (done), ``declined``, ``timed-out`` or ``unknown``.

    ``resume`` is true only for ``resumed``. For ``declined`` and ``timed-out`` ``next`` names what the task must do:
    ``fail``, ``skip``, or ``escalate`` (with ``to``). ``input`` is the person's note and ``by`` who gave it; the
    result never contains authority, so nothing the person typed can widen the task's Tools.
    """
    handoff = _handoff()
    document = handoff.find(root, step_id_for(envelope.get("taskId"), step))
    if document is None or document.get("tool") != TOOL:
        return {"state": "unknown", "resume": False}
    request = document.get("request", {})
    fallback = request.get("fallback", "fail")
    current = int(time.time()) if now is None else now
    base = {"handoffId": document["id"], "assignee": document["assignee"], "deadline": document["deadline"]}
    if document["status"] == "done":
        return {**base, "state": "resumed", "resume": True, "by": document.get("completedBy"), "input": document.get("note"),
                "late": bool(document.get("late"))}
    if document["status"] == "declined":
        return {**base, "state": "declined", "resume": False, "by": document.get("completedBy"), "input": document.get("note"),
                "next": "fail" if fallback == "fail" else fallback, **({"to": request["escalateTo"]} if fallback == "escalate" else {})}
    if current > int(document["deadline"]):
        return {**base, "state": "timed-out", "resume": False, "next": fallback,
                **({"to": request["escalateTo"]} if fallback == "escalate" else {})}
    return {**base, "state": "waiting", "resume": False}


def hop_details(envelope: Mapping[str, Any], step: str, outcome: str, document: Mapping[str, Any]) -> dict[str, Any]:
    note = document.get("note")
    return {
        "taskId": envelope.get("taskId"), "step": step, "handoffId": document.get("id"), "outcome": outcome,
        "assignee": document.get("assignee"), "late": bool(document.get("late")),
        **({"noteDigest": hashlib.sha256(note.encode("utf-8")).hexdigest()} if isinstance(note, str) and note else {}),
        "askDigest": hashlib.sha256(str(document.get("request", {}).get("ask", "")).encode("utf-8")).hexdigest(),
    }


def ledger_opened(govern: Any, audit_path: pathlib.Path | None, envelope: Mapping[str, Any], step: str, document: Mapping[str, Any]) -> dict[str, Any]:
    """Ledger that the task paused on a person, under the Agent's name."""
    return govern.record_operator_action(audit_path, "task.human-step.opened", f"agent:{envelope.get('agent')}",
                                         hop_details(envelope, step, "opened", document))


def ledger_resolved(govern: Any, audit_path: pathlib.Path | None, envelope: Mapping[str, Any], step: str, document: Mapping[str, Any]) -> dict[str, Any]:
    """Ledger the person's answer under *their* name; a timeout is recorded under the task, never as a person."""
    outcome = document.get("status")
    if outcome not in ("done", "declined"):
        raise HumanStepError("only an answered step is ledgered as a person's response")
    return govern.record_operator_action(audit_path, "task.human-step.resolved", str(document.get("completedBy")),
                                         hop_details(envelope, step, str(outcome), document))


def ledger_timed_out(govern: Any, audit_path: pathlib.Path | None, envelope: Mapping[str, Any], step: str, document: Mapping[str, Any]) -> dict[str, Any]:
    return govern.record_operator_action(audit_path, "task.human-step.timed-out", f"task:{envelope.get('taskId')}",
                                         hop_details(envelope, step, "timed-out", document))
