"""Run scheduled and event-triggered coworker tasks as tasks with a ledger timeline, a budget and an owner who is told.

``schedules.py`` stores who asked for what and when; this module is the runner that makes a firing a *task* (#99, #96, #97).
It is credential-free and calls no provider: the coworker itself is an injected ``runner``. The runtime worker
(``platform/runtime/worker.py``) calls ``run_due`` and ``resume_waiting`` on its clock with the coworker (the Hermes API server) as the
runner; a caller calls ``run_event`` when something arrives.

- **A run is a task with its own timeline.** Each firing gets a task id (``<schedule id>:run-<n>``) and writes ``task.start``
  and ``task.finish`` ledger events (identifiers, digests and counts; never the goal, an event body or an error message), plus
  the human-step events when it pauses on a person. ``timeline`` reads one task's events back in order.
- **Authority is the owner's, now.** The envelope is built with ``schedules.run_envelope``: the Agent's grants narrowed to the
  owner's *current* permissions (``permissions_for``). A schedule asking for a Tool the owner no longer holds does not run.
  An event that triggers a run is data: only its kind and a digest of its reference reach the runner, and nothing in it can
  widen authority or choose a recipient.
- **At most once per slot or event.** The slot or event is claimed atomically before the run starts, so two schedulers cannot
  run the same one and a restart never repeats it. If the process dies mid-run the run stays ``running`` in the store and is
  visible; it is not silently retried, because repeating a side effect (sending a summary twice) is worse than a late run.
- **Budget halts the task and tells the owner.** The runner is given a ``BudgetMeter``; charging past the per-run budget
  raises ``BudgetExhausted`` after recording the overspend, the run ends ``failed`` with reason ``budget-exhausted``, a
  ``task.budget-exhausted`` event is ledgered and the owner gets a notice. Usage the runner reports at the end is charged the
  same way.
- **Failures notify the owner, repeated ones suspend the schedule.** A refused authority, a runner error or an exhausted
  budget produces one notice for the owner (identifiers, reason code and next step; never the goal or the error text) and
  counts toward three consecutive failures, after which the schedule is paused with a second notice.
- **A task can pause on a person and resume.** A runner may return ``{"status": "waiting", "step": ..., "assignee": ..., "ask":
  ...}``; the run opens a durable Tier 4 hand-off (``human_step.py``) and ends the call. ``resume_waiting`` reads each waiting
  run's hand-off: the person's answer resumes the runner with their identity and note (the note is data and never widens
  authority); a decline or timeout follows the configured fallback (``fail``, ``skip`` or ``escalate`` once to a named
  person). The person's answer and any timeout are ledgered in the task's timeline.

Owner notices are queued by the worker and delivered through the owner's own channel (``notice_delivery.py``).
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

Audit = Callable[[str, str, dict[str, Any]], Any]
Runner = Callable[[dict[str, Any]], Mapping[str, Any] | None]
FAILURE_TEXT = {
    "authority-refused": "the task needs access its owner or coworker no longer has",
    "budget-exhausted": "it used more than its per-run budget and was stopped",
    "runner-error": "it stopped with an error",
    "human-step-declined": "the person it asked declined",
    "human-step-timed-out": "the person it asked did not answer in time",
    "human-step-failed": "it could not ask a person",
}


class BudgetExhausted(RuntimeError):
    """Raised when a run charges more than its per-run budget; the run must stop."""

    def __init__(self, keys: Iterable[str]) -> None:
        self.keys = sorted(keys)
        super().__init__("per-run budget exceeded: " + ", ".join(self.keys))


def _sibling(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _schedules():
    return _sibling("model_garden_scheduler_schedules", "schedules.py")


def _delegation():
    return _sibling("model_garden_scheduler_delegation", "delegation.py")


def _human_step():
    return _sibling("model_garden_scheduler_human_step", "human_step.py")


def _handoff_module():
    return _sibling("model_garden_scheduler_human_step_handoff", "handoff.py")


class BudgetMeter:
    """Charges a run's usage against its envelope budget. ``charge`` raises ``BudgetExhausted`` once any bound is passed."""

    def __init__(self, envelope: Mapping[str, Any]) -> None:
        self.envelope = dict(envelope)

    def charge(self, **used: int) -> dict[str, int]:
        """Record usage (``tokens``, ``costCents``, ``steps``); returns what is left. Raises when a bound is exceeded."""
        delegation = _delegation()
        try:
            updated = delegation.charge(self.envelope, **used)
        except PermissionError as exc:  # DelegationError: not a valid usage record
            raise ValueError(str(exc)) from exc
        self.envelope = updated
        over = sorted(key for key, amount in updated["budget"].items() if updated["spent"].get(key, 0) > amount)
        if over:
            raise BudgetExhausted(over)
        return delegation.remaining(updated)

    @property
    def spent(self) -> dict[str, int]:
        return dict(self.envelope.get("spent", {}))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _notice(kind: str, entry: Mapping[str, Any], task_id: str, reason: str, text: str) -> dict[str, Any]:
    return {
        "key": _digest(f"{kind}\0{task_id}"), "kind": kind, "to": entry["owner"], "scheduleId": entry["id"], "taskId": task_id,
        "reason": reason, "text": text,
    }


class Scheduler:
    def __init__(
        self,
        store: Any,
        runner: Runner,
        *,
        grants_for: Callable[[str], Iterable[str]],
        permissions_for: Callable[[str], Iterable[str]],
        handoff_root: pathlib.Path | None = None,
        audit: Audit | None = None,
        notify: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._store, self._runner = store, runner
        self._grants_for, self._permissions_for = grants_for, permissions_for
        self._handoff_root, self._audit, self._notify = handoff_root, audit, notify

    # -- ledger and notices --------------------------------------------------------------------

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def _tell_owner(self, notice: dict[str, Any], notices: list[dict[str, Any]]) -> None:
        delivered = False
        if self._notify is not None:
            try:
                self._notify(notice)
                delivered = True
            except Exception:  # noqa: BLE001 - a failed delivery must not lose the notice or stop other runs
                delivered = False
        notices.append({**notice, "delivered": delivered})

    # -- one run ---------------------------------------------------------------------------------

    def _finish(
        self, entry: Mapping[str, Any], task_id: str, *, ok: bool, reason: str | None, now: int, spent: Mapping[str, int],
        notices: list[dict[str, Any]], skipped: bool = False,
    ) -> dict[str, Any]:
        state = "succeeded" if ok else "failed"
        self._store.update_run(task_id, state=state, finishedAt=now, **({"reason": reason} if reason else {}), spent=dict(spent))
        details: dict[str, Any] = {
            "taskId": task_id, "scheduleId": entry["id"], "agent": entry["agent"], "owner": entry["owner"],
            "outcome": state, "spent": dict(spent),
        }
        if reason:
            details["reason"] = reason
        if skipped:
            details["humanStep"] = "skipped"
        self._record("task.finish", f"task:{task_id}", details)
        updated = self._store.mark_outcome(entry["id"], ok=ok, now=now)
        if not ok:
            why = FAILURE_TEXT.get(reason or "", "it did not complete")
            self._tell_owner(_notice(
                "schedule-run-failed", entry, task_id, reason or "failed",
                f"Your scheduled task {entry['id']} did not complete: {why}. It was not retried; check it with your operator.",
            ), notices)
            if updated.get("pausedReason") == "repeated-failures":
                self._tell_owner(_notice(
                    "schedule-suspended", entry, task_id, "repeated-failures",
                    f"Your scheduled task {entry['id']} failed three times in a row and was paused. Resume it when the cause is fixed.",
                ), notices)
        return {"taskId": task_id, "scheduleId": entry["id"], "state": state, **({"reason": reason} if reason else {}), "spent": dict(spent)}

    def _open_step(self, entry: Mapping[str, Any], envelope: Mapping[str, Any], request: Mapping[str, Any], now: int) -> dict[str, Any]:
        if self._handoff_root is None:
            raise RuntimeError("no hand-off store is configured for human steps")
        human = _human_step()
        document = human.open_step(
            self._handoff_root, envelope, step=request["step"], assignee=request["assignee"], ask=request["ask"],
            context=request.get("context", ""), timeout_hours=int(request.get("timeoutHours", 24)),
            fallback=request.get("fallback", "fail"), escalate_to=request.get("escalateTo"), now=now,
        )
        self._record("task.human-step.opened", f"agent:{envelope['agent']}", human.hop_details(envelope, request["step"], "opened", document))
        return document

    def _execute(self, entry: Mapping[str, Any], *, trigger: str, event: Mapping[str, str] | None, now: int,
                 deadline_seconds: int | None, notices: list[dict[str, Any]]) -> dict[str, Any]:
        schedules = _schedules()
        task_id = f"{entry['id']}:run-{entry['runs']}"
        self._store.start_run(task_id, entry, now=now, trigger=trigger)
        start: dict[str, Any] = {
            "taskId": task_id, "scheduleId": entry["id"], "agent": entry["agent"], "owner": entry["owner"],
            "trigger": trigger, "goalDigest": entry["goalDigest"], "budget": entry["budget"],
        }
        if event:
            start["eventDigest"] = event["refDigest"]
        self._record("task.start", f"schedule:{entry['id']}", start)
        try:
            envelope = schedules.run_envelope(
                entry, agent_grants=self._grants_for(entry["agent"]), owner_permissions=self._permissions_for(entry["owner"]),
                run_number=entry["runs"], deadline=(now + deadline_seconds) if deadline_seconds else None,
            )
        except schedules.ScheduleError:
            return self._finish(entry, task_id, ok=False, reason="authority-refused", now=now, spent={}, notices=notices)
        meter = BudgetMeter(envelope)
        task = {"envelope": envelope, "meter": meter, "trigger": {"kind": trigger, **({"event": dict(event)} if event else {})}}
        return self._advance(entry, task, now=now, notices=notices)

    def _advance(self, entry: Mapping[str, Any], task: dict[str, Any], *, now: int, notices: list[dict[str, Any]]) -> dict[str, Any]:
        envelope, meter = task["envelope"], task["meter"]
        task_id = envelope["taskId"]
        try:
            outcome = self._runner(task) or {}
            usage = outcome.get("usage")
            if isinstance(usage, Mapping) and usage:
                meter.charge(**{key: int(value) for key, value in usage.items() if key in ("tokens", "costCents", "steps")})
        except BudgetExhausted as exc:
            self._record("task.budget-exhausted", f"task:{task_id}", {
                "taskId": task_id, "scheduleId": entry["id"], "exceeded": exc.keys, "budget": dict(envelope["budget"]), "spent": meter.spent,
            })
            return self._finish(entry, task_id, ok=False, reason="budget-exhausted", now=now, spent=meter.spent, notices=notices)
        except Exception:  # noqa: BLE001 - any runner failure is a failed run; the error text is never kept
            return self._finish(entry, task_id, ok=False, reason="runner-error", now=now, spent=meter.spent, notices=notices)
        if outcome.get("status") == "waiting":
            try:
                document = self._open_step(entry, envelope, outcome, now)
            except Exception:  # noqa: BLE001
                return self._finish(entry, task_id, ok=False, reason="human-step-failed", now=now, spent=meter.spent, notices=notices)
            self._store.update_run(task_id, state="waiting", step=outcome["step"], handoffId=document["id"], spent=meter.spent,
                                   assignee=outcome["assignee"], escalated=False)
            return {"taskId": task_id, "scheduleId": entry["id"], "state": "waiting", "step": outcome["step"], "spent": meter.spent}
        return self._finish(entry, task_id, ok=True, reason=None, now=now, spent=meter.spent, notices=notices)

    # -- entry points ----------------------------------------------------------------------------

    def run_due(self, *, now: int, limit: int = 20, deadline_seconds: int | None = None) -> dict[str, Any]:
        """Run every cron schedule whose slot has arrived. Returns ``{"runs": [...], "notices": [...]}``."""
        notices: list[dict[str, Any]] = []
        runs = [
            self._execute(entry, trigger="schedule", event=None, now=now, deadline_seconds=deadline_seconds, notices=notices)
            for entry in self._store.claim_due(now=now, limit=limit)
        ]
        return {"runs": runs, "notices": notices}

    def run_event(self, event: str, event_ref: str, *, now: int, self_originated: bool = False, deadline_seconds: int | None = None) -> dict[str, Any]:
        """Run the active triggers for one event. ``self_originated`` events (the coworker's own messages) never trigger."""
        if self_originated:
            return {"runs": [], "notices": [], "limited": [], "duplicate": [], "ignored": "self-originated"}
        claims = self._store.claim_event(event, event_ref, now=now)
        reference = {"kind": event, "refDigest": _digest(event_ref)}
        notices: list[dict[str, Any]] = []
        runs = [
            self._execute(entry, trigger=f"event:{event}", event=reference, now=now, deadline_seconds=deadline_seconds, notices=notices)
            for entry in claims["claimed"]
        ]
        return {"runs": runs, "notices": notices, "limited": [item["id"] for item in claims["limited"]],
                "duplicate": [item["id"] for item in claims["duplicate"]]}

    def resume_waiting(self, *, now: int) -> dict[str, Any]:
        """Resume, fail or escalate every run waiting on a person, according to its hand-off. Returns the outcomes."""
        human, schedules = _human_step(), _schedules()
        notices: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        for record in self._store.runs(states=("waiting",)):
            entry = self._store.get(record["scheduleId"])
            if entry is None:
                self._store.update_run(record["taskId"], state="failed", finishedAt=now, reason="schedule-deleted")
                continue
            try:
                envelope = schedules.run_envelope(
                    entry, agent_grants=self._grants_for(entry["agent"]), owner_permissions=self._permissions_for(entry["owner"]),
                    run_number=int(record["taskId"].rsplit("-", 1)[1]),
                )
            except schedules.ScheduleError:
                outcomes.append(self._finish(entry, record["taskId"], ok=False, reason="authority-refused", now=now, spent=record.get("spent", {}), notices=notices))
                continue
            envelope["spent"] = dict(record.get("spent", {}))
            step = record["step"]
            result = human.resolution(self._handoff_root, envelope, step, now=now)
            document = _handoff_document(self._handoff_root, result)
            if result["state"] == "waiting":
                outcomes.append({"taskId": record["taskId"], "state": "waiting", "step": step})
                continue
            if result["state"] in ("resumed", "declined"):
                if document is not None:
                    self._record("task.human-step.resolved", str(document.get("completedBy")), human.hop_details(envelope, step, str(document["status"]), document))
            elif document is not None:
                self._record("task.human-step.timed-out", f"task:{record['taskId']}", human.hop_details(envelope, step, "timed-out", document))
                # The task has moved on, so the stale hand-off is closed rather than left pending in the person's inbox.
                _handoff_module().complete(self._handoff_root, document["id"], "declined", "runtime:scheduler", note="timed out", now=now)
            meter = BudgetMeter(envelope)
            if result["state"] == "resumed":
                task = {"envelope": envelope, "meter": meter, "trigger": {"kind": record["trigger"]},
                        "resumed": {"step": step, "by": result.get("by"), "input": result.get("input"), "late": result.get("late", False)}}
                outcomes.append(self._advance(entry, task, now=now, notices=notices))
                continue
            reason = "human-step-declined" if result["state"] == "declined" else "human-step-timed-out"
            if result.get("next") == "skip":
                task = {"envelope": envelope, "meter": meter, "trigger": {"kind": record["trigger"]}, "resumed": {"step": step, "skipped": True}}
                outcomes.append(self._advance(entry, task, now=now, notices=notices))
            elif result.get("next") == "escalate" and not record.get("escalated"):
                try:
                    escalated = self._open_step(entry, envelope, {
                        "step": f"{step}:escalated", "assignee": result["to"], "ask": f"Escalated: {document['request']['ask']}" if document else "Escalated step",
                        "fallback": "fail", "timeoutHours": 24,
                    }, now)
                    self._store.update_run(record["taskId"], step=f"{step}:escalated", handoffId=escalated["id"], assignee=result["to"], escalated=True)
                    outcomes.append({"taskId": record["taskId"], "state": "waiting", "step": f"{step}:escalated"})
                except Exception:  # noqa: BLE001
                    outcomes.append(self._finish(entry, record["taskId"], ok=False, reason="human-step-failed", now=now, spent=record.get("spent", {}), notices=notices))
            else:
                outcomes.append(self._finish(entry, record["taskId"], ok=False, reason=reason, now=now, spent=record.get("spent", {}), notices=notices))
        return {"runs": outcomes, "notices": notices}


def _handoff_document(root: pathlib.Path | None, result: Mapping[str, Any]) -> dict[str, Any] | None:
    handoff_id = result.get("handoffId")
    if root is None or not handoff_id:
        return None
    return _handoff_module().find(root, handoff_id)


def timeline(events: Iterable[Mapping[str, Any]], task_id: str) -> list[dict[str, Any]]:
    """One task's ledger events in order: ``task.*`` operator actions whose details name the task, with the sidecar's own
    events for it (any event whose governance context carries this ``taskId``)."""
    selected = []
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
        governance = event.get("governance") if isinstance(event.get("governance"), Mapping) else {}
        if details.get("taskId") == task_id or governance.get("taskId") == task_id:
            selected.append(dict(event))
    return sorted(selected, key=lambda item: item.get("sequence", 0))
