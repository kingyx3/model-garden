"""Each Agent's inbox of delegated work, so handing a task over works the way it does between colleagues (#95, #96, #98).

A task handed to an Agent, by a person or by another Agent, waits in that Agent's inbox until the Agent takes it up. The Agent can finish
it, decline it or say it failed, and the one who handed it over can read the answer. This module is only that record; the rules for who
may hand what to whom, and the budget, stay in ``delegation.py`` and ``task_budget.py`` and are applied by ``TeamRuntime``.

- **Who can see what.** An Agent sees only the tasks addressed to it. The assigner (a person or the delegating Agent) and the task's
  originating person see a task's state and report; nobody else does.
- **A report is data.** The text an Agent reports back is bounded and never executed or trusted as instructions by whoever reads it; the
  ledger holds its length and digest, never the text.
- **Bounded.** Goals and reports are short, and the oldest finished entries are dropped past a fixed count, so an inbox cannot grow
  without limit.

Pure Python over one private JSON file with a lock; no credentials, no network.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
STATES = ("assigned", "working", "done", "declined", "failed")
OPEN_STATES = ("assigned", "working")
OUTCOMES = ("done", "declined", "failed")
MAX_GOAL = 500
MAX_REPORT = 2000
MAX_ENTRIES = 1000
_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class InboxError(ValueError):
    """The request is malformed or names a task that is not this Agent's; the message is safe to show."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class TaskInbox:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tasks": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise InboxError("the task inbox has an unsupported format")
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

    @staticmethod
    def _id(task_id: Any) -> str:
        if not isinstance(task_id, str) or not _TASK.match(task_id):
            raise InboxError("a task id is a short identifier")
        return task_id

    def assign(self, agent: str, task_id: str, *, assigner: str, goal: str, root: str, parent: str | None, now: int) -> dict[str, Any]:
        """Put a task in ``agent``'s inbox. ``assigner`` is ``person:<name>`` or ``agent:<name>``."""
        if not isinstance(agent, str) or not _NAME.match(agent):
            raise InboxError("an Agent name is a short lowercase identifier")
        if not isinstance(assigner, str) or not re.match(r"^(person|agent):[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$", assigner):
            raise InboxError("the assigner is person:<name> or agent:<name>")
        if not isinstance(goal, str) or not goal.strip():
            raise InboxError("a task needs a goal")
        task_id = self._id(task_id)
        entry = {"taskId": task_id, "agent": agent, "assigner": assigner, "goal": goal.strip()[:MAX_GOAL], "root": self._id(root),
                 "parent": self._id(parent) if parent else None, "state": "assigned", "report": "", "createdAt": int(now), "updatedAt": int(now)}
        with self._locked() as state:
            if task_id in state["tasks"]:
                raise InboxError("that task id is already in an inbox")
            state["tasks"][task_id] = entry
            if len(state["tasks"]) > MAX_ENTRIES:
                finished = sorted((e for e in state["tasks"].values() if e["state"] not in OPEN_STATES), key=lambda e: e["updatedAt"])
                for old in finished[: len(state["tasks"]) - MAX_ENTRIES]:
                    del state["tasks"][old["taskId"]]
        return dict(entry)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._locked() as state:
            entry = state["tasks"].get(task_id) if isinstance(task_id, str) else None
            return dict(entry) if entry else None

    def pending(self, agent: str) -> list[dict[str, Any]]:
        """The open tasks addressed to ``agent``, oldest first."""
        with self._locked() as state:
            found = [dict(e) for e in state["tasks"].values() if e["agent"] == agent and e["state"] in OPEN_STATES]
        return sorted(found, key=lambda e: (e["createdAt"], e["taskId"]))

    def take_up(self, agent: str, task_id: str, *, now: int) -> dict[str, Any]:
        """``agent`` starts working on a task addressed to it."""
        with self._locked() as state:
            entry = self._mine(state, agent, task_id)
            if entry["state"] not in OPEN_STATES:
                raise InboxError(f"that task is already {entry['state']}")
            entry["state"], entry["updatedAt"] = "working", int(now)
            return dict(entry)

    def report(self, agent: str, task_id: str, outcome: str, text: str, *, now: int) -> dict[str, Any]:
        """``agent`` finishes, declines or fails a task addressed to it, with a short report for whoever handed it over."""
        if outcome not in OUTCOMES:
            raise InboxError("the outcome is done, declined or failed")
        if not isinstance(text, str):
            raise InboxError("the report is text")
        with self._locked() as state:
            entry = self._mine(state, agent, task_id)
            if entry["state"] not in OPEN_STATES:
                raise InboxError(f"that task is already {entry['state']}")
            entry.update(state=outcome, report=text.strip()[:MAX_REPORT], updatedAt=int(now))
            return dict(entry)

    def read(self, task_id: str, *, assigner: str | None = None, person: str | None = None, root: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """A task's state and report for the one who handed it over, or the person it is ultimately for; anyone else gets nothing."""
        entry = self.get(task_id)
        if entry is None:
            raise InboxError("no such task")
        allowed = (assigner is not None and entry["assigner"] == assigner) or (
            person is not None and (entry["assigner"] == f"person:{person}" or (root or {}).get("originatingPrincipal") == person))
        if not allowed:
            raise InboxError("no such task")  # the same answer as a missing one, so a task id says nothing to a stranger
        return entry

    @staticmethod
    def _mine(state: Mapping[str, Any], agent: str, task_id: Any) -> dict[str, Any]:
        entry = state["tasks"].get(task_id) if isinstance(task_id, str) else None
        if entry is None or entry["agent"] != agent:
            raise InboxError("no such task in your inbox")
        return entry
