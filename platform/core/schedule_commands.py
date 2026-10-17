"""Owner chat commands for schedules: list, create, pause, resume and delete, from a verified principal (#99).

A person who asked "send me a pipeline summary every Monday" should be able to see, stop and remove that without an operator.
This is the credential-free command layer on top of ``schedules.ScheduleStore``. It parses one narrow, explicit syntax and
returns the plain-text reply for the channel; anything that is not one of these commands returns ``None`` so the message
goes to the coworker as usual. The channel gateway (``channels/ingress.py``) routes a verified message to it for a mapped person; there is no natural-language parsing and
no real channel has had live proof.

    schedules                                  list your schedules
    schedule create <cron> | <budget> | <goal>
    trigger create <event> | <budget> | <goal>
    schedule pause|resume|delete <id>

``<budget>`` is ``key=number`` pairs from ``tokens``, ``costCents`` and ``steps`` (for example ``steps=20 tokens=50000``);
``<event>`` is ``mail.received``, ``ticket.created`` or ``chat.message``.

- **The sender is the verified principal.** The caller passes the principal its channel adapter verified; nothing in the
  message can name a different owner, and an unverified or empty principal is refused. The store still enforces owner-only
  control, so one person cannot pause or delete another's schedule (an administrator flag is the caller's decision, never the
  message's).
- **Creating is not granting.** A schedule made from chat has the owner as its named owner and a required budget; every run's
  authority is still the Agent's grants narrowed to the owner's permissions at run time, so a command cannot create access.
- **Goals are data.** The text after the last ``|`` is stored as the goal and never parsed as a command; a goal that itself
  looks like a command is just a goal. Replies show at most a short preview of the owner's own goal and never anything
  about someone else's schedule: another person's id gets the same answer as one that does not exist.
- **Errors say what to fix.** A bad cron, budget or id gets a one-line reason, never a stack trace or another person's data.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Any

GOAL_PREVIEW = 60
_ID = re.compile(r"^s-[0-9a-f]{12}$")
_COMMAND = re.compile(r"^\s*(schedules\b|schedule\s+(?:create|pause|resume|delete)\b|trigger\s+create\b)", re.IGNORECASE)
_BUDGET = re.compile(r"^(tokens|costCents|steps)=(\d{1,12})$")


def _store_module():
    name = "model_garden_schedule_commands_store"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("schedules.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def is_command(text: Any) -> bool:
    return isinstance(text, str) and bool(_COMMAND.match(text))


def _budget(text: str) -> dict[str, int]:
    budget: dict[str, int] = {}
    for token in text.split():
        match = _BUDGET.match(token)
        if not match or match.group(1) in budget:
            raise ValueError("the budget is key=number pairs from tokens, costCents and steps, for example steps=20 tokens=50000")
        budget[match.group(1)] = int(match.group(2))
    if not budget:
        raise ValueError("a schedule needs a budget, for example steps=20")
    return budget


def _describe(entry: Mapping[str, Any]) -> str:
    when = f"on {entry['event']}" if entry.get("kind") == "trigger" else f"cron {entry['cron']}"
    goal = str(entry["goal"])
    preview = goal if len(goal) <= GOAL_PREVIEW else goal[: GOAL_PREVIEW - 1] + "…"
    reason = f" ({entry['pausedReason']})" if entry.get("pausedReason") else ""
    return f"{entry['id']}  {entry['state']}{reason}  {when}  runs={entry.get('runs', 0)}  \"{preview}\""


def handle(
    store: Any,
    text: str,
    *,
    principal: str | None,
    agent: str,
    admin: bool = False,
    now: int | None = None,
) -> str | None:
    """Run one schedule command for ``principal`` and return the reply, or ``None`` if ``text`` is not a schedule command."""
    if not is_command(text):
        return None
    schedules = _store_module()
    if not isinstance(principal, str) or not principal:
        return "I can only manage schedules for a verified person."
    try:
        words = text.strip().split(None, 2)
        verb = words[0].lower()
        if verb == "schedules":
            if len(words) != 1:
                return "Use just `schedules` to list yours."
            mine = store.list(owner=principal)
            return "You have no schedules." if not mine else "Your schedules:\n" + "\n".join(_describe(item) for item in mine)
        action = words[1].lower() if len(words) > 1 else ""
        rest = words[2] if len(words) > 2 else ""
        if action == "create":
            parts = [part.strip() for part in rest.split("|", 2)]
            if len(parts) != 3 or not all(parts):
                shape = "schedule create <cron> | <budget> | <goal>" if verb == "schedule" else "trigger create <event> | <budget> | <goal>"
                return f"Use: {shape}"
            when, budget_text, goal = parts
            budget = _budget(budget_text)
            if verb == "trigger":
                entry = store.create_trigger(owner=principal, agent=agent, goal=goal, event=when, budget=budget, now=now)
            else:
                entry = store.create(owner=principal, agent=agent, goal=goal, cron=when, budget=budget, now=now)
            return f"Created {entry['id']}: {_describe(entry)}"
        schedule_id = rest.strip()
        if not _ID.match(schedule_id):
            return f"Use: schedule {action} <id> (see `schedules` for ids)."
        entry = store.get(schedule_id)
        if entry is None or (entry["owner"] != principal and not admin):
            return f"Not done: no such schedule of yours ({schedule_id})."  # the same answer whether it exists or not
        if action == "pause":
            store.pause(schedule_id, requested_by=principal, admin=admin)
            return f"Paused {schedule_id}."
        if action == "resume":
            store.resume(schedule_id, requested_by=principal, admin=admin, now=now)
            return f"Resumed {schedule_id}."
        store.delete(schedule_id, requested_by=principal, admin=admin)
        return f"Deleted {schedule_id}."
    except schedules.ScheduleError as exc:
        return f"Not done: {exc}."
    except ValueError as exc:
        return f"Not done: {exc}."
