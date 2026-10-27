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
import secrets
import stat
from collections.abc import Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
STATES = ("assigned", "working", "done", "declined", "failed")
OPEN_STATES = ("assigned", "working")
OUTCOMES = ("done", "declined", "failed")
MAX_GOAL = 500
MAX_REPORT = 2000
MAX_ENTRIES = 1000
MAX_STATE_BYTES = 8 * 1024 * 1024
_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class InboxError(ValueError):
    """The request is malformed or names a task that is not this Agent's; the message is safe to show."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class TaskInbox:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def _read_state(self, directory: int) -> tuple[dict[str, Any], tuple[int, int] | None]:
        """Read a bounded, private, pinned regular inode; never trust a linked task file."""
        empty = {"schemaVersion": SCHEMA_VERSION, "tasks": {}}
        try:
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=directory)
        except FileNotFoundError:
            return empty, None
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) & 0o077 or before.st_size > MAX_STATE_BYTES):
                raise InboxError("the task inbox is not a private regular file")
            raw = os.read(descriptor, MAX_STATE_BYTES + 1)
            after = os.fstat(descriptor)
            current = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
            if (len(raw) > MAX_STATE_BYTES or identity(before) != identity(after)
                    or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
                raise InboxError("the task inbox changed while being read")
            try:
                loaded = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise InboxError("the task inbox has invalid content") from exc
            if (not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION
                    or not isinstance(loaded.get("tasks"), dict)):
                raise InboxError("the task inbox has an unsupported format")
            for key, row in loaded["tasks"].items():
                if (not isinstance(key, str) or not _TASK.fullmatch(key) or not isinstance(row, dict)
                        or row.get("taskId") != key or row.get("state") not in STATES
                        or not isinstance(row.get("agent"), str) or not isinstance(row.get("assigner"), str)
                        or not isinstance(row.get("goal"), str) or not isinstance(row.get("report"), str)):
                    raise InboxError("the task inbox contains an invalid task")
            return loaded, (before.st_dev, before.st_ino)
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Serialize processes and durably publish changes through a pinned private directory."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        lock = None
        temporary = None
        try:
            pinned = os.fstat(directory)
            lock_name = self.path.with_suffix(".lock").name
            lock = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600, dir_fd=directory)
            locked = os.fstat(lock)
            if (not stat.S_ISREG(locked.st_mode) or locked.st_uid != os.getuid() or locked.st_nlink != 1
                    or stat.S_IMODE(locked.st_mode) & 0o077):
                raise InboxError("the task inbox lock is not a private regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            current_lock = os.stat(lock_name, dir_fd=directory, follow_symlinks=False)
            if (locked.st_dev, locked.st_ino) != (current_lock.st_dev, current_lock.st_ino):
                raise InboxError("the task inbox lock was replaced")
            state, prior = self._read_state(directory)
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                payload = (json.dumps(state, sort_keys=True) + "\n").encode("utf-8")
                if len(payload) > MAX_STATE_BYTES:
                    raise InboxError("the task inbox exceeds its size limit")
                for _ in range(5):
                    candidate = ".inbox-" + secrets.token_hex(12)
                    try:
                        handle = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         0o600, dir_fd=directory)
                        temporary = candidate
                        break
                    except FileExistsError:
                        continue
                else:
                    raise InboxError("cannot allocate a task inbox temporary file")
                with os.fdopen(handle, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                current_dir = os.stat(self.path.parent, follow_symlinks=False)
                if (pinned.st_dev, pinned.st_ino) != (current_dir.st_dev, current_dir.st_ino):
                    raise InboxError("the task inbox directory was replaced")
                try:
                    current = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if (prior is None and current is not None) or (prior is not None and
                        (current is None or (current.st_dev, current.st_ino) != prior)):
                    raise InboxError("the task inbox was replaced during the transaction")
                os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                temporary = None
                os.fsync(directory)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            if lock is not None:
                os.close(lock)
            os.close(directory)

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

    def reported(self) -> list[dict[str, Any]]:
        """Tasks a person handed over whose Agent has reported (done, declined or failed), oldest report first. No report text."""
        with self._locked() as state:
            found = [{"taskId": e["taskId"], "agent": e["agent"], "state": e["state"], "person": e["assigner"][len("person:"):], "updatedAt": e["updatedAt"]}
                     for e in state["tasks"].values() if e["state"] in OUTCOMES and e["assigner"].startswith("person:")]
        return sorted(found, key=lambda e: (e["updatedAt"], e["taskId"]))

    def take_up(self, agent: str, task_id: str, *, now: int) -> dict[str, Any]:
        """``agent`` starts working on a task addressed to it."""
        with self._locked() as state:
            entry = self._mine(state, agent, task_id)
            if entry["state"] != "assigned":
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
