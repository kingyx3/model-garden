"""Scheduled coworker tasks with a named owner, a per-run budget and owner-only controls.

"Send me a pipeline summary every Monday" must keep working without anyone prompting the coworker, and it
must never become a way to do more than the person who asked could do. This module is the contract for that
(#99); it is a credential-free store and cron arithmetic, **not a scheduler**. Nothing here wakes a coworker, and chat
commands are not part of it.

- **A named owner bounds every run.** A schedule belongs to one principal. A run's task envelope comes from
  ``delegation.root_envelope`` with that owner as the originating principal, so the run's authority is the
  Agent's grants narrowed to the owner's permissions *at run time*: when the owner loses a permission, the next
  run loses it too, and a schedule asking for a Tool the owner does not hold is refused instead of run.
- **A budget is required** (``tokens``, ``costCents`` and/or ``steps``, each at least 1), so an unattended run
  has a bound the first time it fires.
- **Cron-like, UTC, at most once per slot.** Five fields (minute hour day-of-month month day-of-week) with
  ``*``, lists, ranges and ``/step``. Fires closer together than 15 minutes are refused. After downtime a
  schedule fires once for the slots it missed, not once per missed slot.
- **Owners control their own.** Only the owner (or an administrator) pauses, resumes or deletes a schedule.
  Listing is by owner. Every change calls the audit hook with identifiers and a digest of the goal, never
  the goal.
- **Per tenant, private, bounded.** One file per tenant (mode 0600 in a 0700 directory, atomic writes under a
  lock); another tenant's file is refused; at most 20 schedules per owner and 500 per tenant.

- **Event triggers share the store.** A trigger (``create_trigger``) is an entry with an event kind (``mail.received``,
  ``ticket.created`` or ``chat.message``) instead of a cron. It has the same owner, budget and owner-only controls, a
  per-hour run limit (default 6, at most 60) so a burst of events cannot become a flood of runs, and it never runs twice for
  the same event: the event reference is kept as a digest. The event itself is data, never authority; only its kind and a
  digest of its reference go to the run.
- **Atomic claims.** ``claim_due`` and ``claim_event`` take a slot or an event under the store's lock, so two processes can
  never both run the same slot. ``mark_outcome`` counts consecutive failures and suspends a schedule after three, and
  ``start_run`` / ``update_run`` / ``runs`` keep a bounded, durable record of each run for ``scheduler.py``.

- **Workspace-managed schedules.** ``reconcile`` makes the store match a list of named definitions (the client's desired state,
  for example a file kept next to the workspace): a missing one is created, a changed one is updated in place (same id, same
  run history), and a *managed* one that is no longer listed is **paused** (``pausedReason: removed-from-workspace``), never
  deleted. A schedule someone created from chat is not managed and is never touched. ``export_desired`` writes the managed
  schedules back out in the same shape, so the state survives a redeploy and round-trips. Every change is ledgered as
  identifiers and digests.

Running each firing as a task (with its own ledger timeline), failure notification to the owner and pausing on a person are
done by ``scheduler.py``; this module is only the store and cron arithmetic.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import secrets
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
MAX_PER_OWNER = 20
MAX_SCHEDULES = 500
MAX_GOAL = 500
MIN_INTERVAL_SECONDS = 15 * 60
BUDGET_KEYS = ("tokens", "costCents", "steps")
EVENT_KINDS = ("mail.received", "ticket.created", "chat.message")
DEFAULT_TRIGGER_PER_HOUR = 6
MAX_TRIGGER_PER_HOUR = 60
MAX_SEEN_EVENTS = 200
MAX_FAILURES = 3
MAX_RUNS = 1000
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
Audit = Callable[[str, str, dict[str, Any]], Any]


class ScheduleError(ValueError):
    """Raised for an invalid schedule or an operation outside the caller's rights."""


def _delegation():
    name = "model_garden_schedules_delegation"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("delegation.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


# -- cron arithmetic ----------------------------------------------------------------------


def _field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or not int(raw_step):
                raise ScheduleError("a cron step must be a positive integer")
            step = int(raw_step)
        if part == "*":
            start, stop = low, high
        elif re.fullmatch(r"\d+", part):
            start = stop = int(part)
        elif re.fullmatch(r"\d+-\d+", part):
            start, _, end = part.partition("-")
            start, stop = int(start), int(end)
        else:
            raise ScheduleError("a cron field is *, numbers, ranges, lists or steps")
        if not low <= start <= stop <= high:
            raise ScheduleError(f"a cron value must be from {low} to {high}")
        values.update(range(start, stop + 1, step))
    return frozenset(values)


def parse_cron(expression: str) -> tuple[frozenset[int], ...]:
    """Minute, hour, day-of-month, month and day-of-week sets (Sunday is 0 or 7)."""
    if not isinstance(expression, str) or len(expression) > 100:
        raise ScheduleError("a cron expression has five fields")
    parts = expression.split()
    if len(parts) != 5:
        raise ScheduleError("a cron expression has five fields: minute hour day-of-month month day-of-week")
    fields = [_field(text, low, high) for text, (low, high) in zip(parts, _FIELD_RANGES)]
    fields[4] = frozenset(day % 7 for day in fields[4])
    fields.append(frozenset({parts[2] != "*" and parts[2][0] != "*"}))  # marker: day-of-month restricted
    fields.append(frozenset({parts[4] != "*" and parts[4][0] != "*"}))  # marker: day-of-week restricted
    return tuple(fields)


def _day_matches(parsed: tuple[frozenset[int], ...], moment: dt.datetime) -> bool:
    _, _, days, months, weekdays, dom_limited, dow_limited = parsed
    if moment.month not in months:
        return False
    in_dom, in_dow = moment.day in days, (moment.isoweekday() % 7) in weekdays
    if True in dom_limited and True in dow_limited:
        return in_dom or in_dow  # standard cron: either restriction may match
    return in_dom and in_dow


def next_fire(expression: str, after: int) -> int | None:
    """The first slot strictly after ``after`` (epoch seconds, UTC), or ``None`` if none exists within 5 years."""
    parsed = parse_cron(expression)
    minutes, hours = parsed[0], parsed[1]
    moment = dt.datetime.fromtimestamp(after, dt.timezone.utc).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    limit = moment + dt.timedelta(days=366 * 5)
    while moment < limit:
        if not _day_matches(parsed, moment):
            moment = (moment + dt.timedelta(days=1)).replace(hour=0, minute=0)
        elif moment.hour not in hours:
            moment = (moment + dt.timedelta(hours=1)).replace(minute=0)
        elif moment.minute not in minutes:
            moment += dt.timedelta(minutes=1)
        else:
            return int(moment.timestamp())
    return None


def _check_interval(expression: str) -> None:
    anchor = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    end = anchor + 31 * 86400
    previous: int | None = None
    current = next_fire(expression, anchor - 60)
    while current is not None and current < end:
        if previous is not None and current - previous < MIN_INTERVAL_SECONDS:
            raise ScheduleError("a schedule may not fire more often than every 15 minutes")
        previous, current = current, next_fire(expression, current)


# -- store --------------------------------------------------------------------------------


def _principal(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PRINCIPAL.match(value):
        raise ScheduleError(f"{label} must be a short identity")
    return value


def _budget(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise ScheduleError("a schedule needs a per-run budget (tokens, costCents and/or steps)")
    unknown = sorted(set(value) - set(BUDGET_KEYS))
    if unknown:
        raise ScheduleError("budget has unsupported keys: " + ", ".join(unknown))
    result: dict[str, int] = {}
    for key, amount in value.items():
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
            raise ScheduleError(f"budget.{key} must be an integer of at least 1")
        result[key] = amount
    return result


def definition_from_resource(resource: Mapping[str, Any]) -> dict[str, Any]:
    """A ``Schedule`` workspace resource as the definition ``reconcile`` accepts (the resource name is the managed name)."""
    spec = dict(resource.get("spec") or {})
    spec.setdefault("requiredCapabilities", [])  # the same shape ``export_desired`` writes, so a round trip is exact
    return {"name": resource["metadata"]["name"], **spec}


def validate_definition(definition: Any) -> dict[str, Any]:
    """The checks a stored schedule gets, without any store: unknown keys, name, owner, budget, cron (and its interval) or event."""
    fields = ScheduleStore._normalised(definition)
    if fields.get("cron") and next_fire(fields["cron"], int(dt.datetime.now(dt.timezone.utc).timestamp())) is None:
        raise ScheduleError("this cron expression never fires")
    return fields


def goal_digest(goal: str) -> str:
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()


class ScheduleStore:
    def __init__(self, root: pathlib.Path, *, tenant: str, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise ScheduleError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.schedules.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / f".{self.tenant}.schedules.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "schedules": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise ScheduleError("schedule store has an unsupported format or belongs to another tenant")
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

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def create(
        self,
        *,
        owner: str,
        agent: str,
        goal: str,
        cron: str,
        budget: Mapping[str, int],
        required_capabilities: Iterable[str] = (),
        now: int | None = None,
    ) -> dict[str, Any]:
        """Add an active schedule for ``owner``; it first fires at the next slot after ``now``."""
        owner = _principal(owner, "owner")
        if not isinstance(agent, str) or not _AGENT.match(agent):
            raise ScheduleError("agent must be a short Agent name")
        if not isinstance(goal, str) or not goal.strip() or len(goal.strip()) > MAX_GOAL:
            raise ScheduleError(f"goal must be a non-empty string of at most {MAX_GOAL} characters")
        parse_cron(cron)
        _check_interval(cron)
        cleaned_budget = _budget(budget)
        try:
            required = sorted(_delegation()._tools(required_capabilities, "required capabilities"))
        except PermissionError as exc:
            raise ScheduleError(str(exc)) from exc
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        upcoming = next_fire(cron, stamp)
        if upcoming is None:
            raise ScheduleError("this cron expression never fires")
        with self._locked() as state:
            schedules = state["schedules"]
            if len(schedules) >= MAX_SCHEDULES:
                raise ScheduleError("the tenant has the maximum number of schedules")
            if sum(1 for item in schedules.values() if item["owner"] == owner) >= MAX_PER_OWNER:
                raise ScheduleError(f"an owner may have at most {MAX_PER_OWNER} schedules")
            entry = {
                "id": "s-" + secrets.token_hex(6), "tenant": self.tenant, "owner": owner, "agent": agent, "goal": goal.strip(),
                "goalDigest": goal_digest(goal.strip()), "cron": " ".join(cron.split()), "budget": cleaned_budget,
                "requiredCapabilities": required, "state": "active", "createdAt": stamp, "nextFire": upcoming, "runs": 0,
            }
            schedules[entry["id"]] = entry
        self._record("schedule.create", owner, self._details(entry))
        return dict(entry)

    @staticmethod
    def _details(entry: Mapping[str, Any]) -> dict[str, Any]:
        if entry.get("kind") == "trigger":
            return {"scheduleId": entry["id"], "agent": entry["agent"], "event": entry["event"], "goalDigest": entry["goalDigest"],
                    "budget": entry["budget"]}
        return {"scheduleId": entry["id"], "agent": entry["agent"], "cron": entry["cron"], "goalDigest": entry["goalDigest"],
                "budget": entry["budget"]}

    def list(self, *, owner: str | None = None) -> list[dict[str, Any]]:
        with self._locked() as state:
            entries = [dict(item) for item in state["schedules"].values()]
        if owner is not None:
            entries = [item for item in entries if item["owner"] == owner]
        return sorted(entries, key=lambda item: (item["createdAt"], item["id"]))

    def _change(self, schedule_id: str, requested_by: str, admin: bool, action: str, state_after: str | None) -> dict[str, Any]:
        who = _principal(requested_by, "requested_by")
        now_state = None
        with self._locked() as state:
            entry = state["schedules"].get(schedule_id)
            if entry is None:
                raise ScheduleError("no such schedule")
            if not admin and entry["owner"] != who:
                raise ScheduleError("only the owner of a schedule or an administrator may change it")
            if state_after is None:
                del state["schedules"][schedule_id]
            else:
                entry["state"] = state_after
                now_state = dict(entry)
        self._record(action, who, {**self._details(entry), "admin": bool(admin)})
        return now_state or {"deleted": schedule_id}

    def edit(self, schedule_id: str, *, by: str, cron: str | None = None, budget: Mapping[str, Any] | None = None, now: int | None = None) -> bool:
        """Set a timed schedule's cron and/or per-run budget to approved values (the Studio's approved edit); False when nothing differs.

        The values are checked with the same rules a new schedule gets. A changed cron moves the next fire; the owner, goal and state
        are untouched. A trigger has no cron to edit.
        """
        who = _principal(by, "by")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        new_cron = None
        if cron is not None:
            if not isinstance(cron, str):
                raise ScheduleError("cron must be a string")
            parse_cron(cron)
            _check_interval(cron)
            new_cron = " ".join(cron.split())
            if next_fire(new_cron, stamp) is None:
                raise ScheduleError("this cron expression never fires")
        new_budget = _budget(budget) if budget is not None else None
        with self._locked() as state:
            entry = state["schedules"].get(schedule_id)
            if entry is None:
                raise ScheduleError("no such schedule")
            if new_cron is not None and entry.get("kind") == "trigger":
                raise ScheduleError("an event trigger has no cron to edit")
            changed = False
            if new_cron is not None and entry.get("cron") != new_cron:
                entry["cron"], changed = new_cron, True
                entry["nextFire"] = next_fire(new_cron, stamp)
            if new_budget is not None and entry.get("budget") != new_budget:
                entry["budget"], changed = new_budget, True
            snapshot = dict(entry)
        if changed:
            self._record("schedule.update", who, {**self._details(snapshot), "managedBy": snapshot.get("managedBy")})
        return changed

    def pause(self, schedule_id: str, *, requested_by: str, admin: bool = False) -> dict[str, Any]:
        return self._change(schedule_id, requested_by, admin, "schedule.pause", "paused")

    def resume(self, schedule_id: str, *, requested_by: str, admin: bool = False, now: int | None = None) -> dict[str, Any]:
        """Re-activate; the next slot is computed from now, so a long pause never causes a burst of runs."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        who = _principal(requested_by, "requested_by")
        with self._locked() as state:
            entry = state["schedules"].get(schedule_id)
            if entry is None:
                raise ScheduleError("no such schedule")
            if not admin and entry["owner"] != who:
                raise ScheduleError("only the owner of a schedule or an administrator may change it")
            entry["state"] = "active"
            entry.pop("pausedReason", None)
            entry["consecutiveFailures"] = 0
            if entry.get("cron"):
                entry["nextFire"] = next_fire(entry["cron"], stamp)
            result = dict(entry)
        self._record("schedule.resume", who, {**self._details(result), "admin": bool(admin)})
        return result

    def delete(self, schedule_id: str, *, requested_by: str, admin: bool = False) -> dict[str, Any]:
        return self._change(schedule_id, requested_by, admin, "schedule.delete", None)

    def create_trigger(
        self,
        *,
        owner: str,
        agent: str,
        goal: str,
        event: str,
        budget: Mapping[str, int],
        required_capabilities: Iterable[str] = (),
        max_per_hour: int = DEFAULT_TRIGGER_PER_HOUR,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Add an active event trigger for ``owner``: it runs once for each matching event, up to ``max_per_hour`` an hour."""
        owner = _principal(owner, "owner")
        if not isinstance(agent, str) or not _AGENT.match(agent):
            raise ScheduleError("agent must be a short Agent name")
        if not isinstance(goal, str) or not goal.strip() or len(goal.strip()) > MAX_GOAL:
            raise ScheduleError(f"goal must be a non-empty string of at most {MAX_GOAL} characters")
        if event not in EVENT_KINDS:
            raise ScheduleError("event must be one of " + ", ".join(EVENT_KINDS))
        if not isinstance(max_per_hour, int) or isinstance(max_per_hour, bool) or not 1 <= max_per_hour <= MAX_TRIGGER_PER_HOUR:
            raise ScheduleError(f"max_per_hour must be an integer from 1 to {MAX_TRIGGER_PER_HOUR}")
        cleaned_budget = _budget(budget)
        try:
            required = sorted(_delegation()._tools(required_capabilities, "required capabilities"))
        except PermissionError as exc:
            raise ScheduleError(str(exc)) from exc
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        with self._locked() as state:
            schedules = state["schedules"]
            if len(schedules) >= MAX_SCHEDULES:
                raise ScheduleError("the tenant has the maximum number of schedules")
            if sum(1 for item in schedules.values() if item["owner"] == owner) >= MAX_PER_OWNER:
                raise ScheduleError(f"an owner may have at most {MAX_PER_OWNER} schedules")
            entry = {
                "id": "s-" + secrets.token_hex(6), "kind": "trigger", "tenant": self.tenant, "owner": owner, "agent": agent,
                "goal": goal.strip(), "goalDigest": goal_digest(goal.strip()), "event": event, "cron": None, "nextFire": None,
                "maxPerHour": max_per_hour, "budget": cleaned_budget, "requiredCapabilities": required, "state": "active",
                "createdAt": stamp, "runs": 0, "recentFires": [], "seenEvents": [],
            }
            schedules[entry["id"]] = entry
        self._record("schedule.create", owner, self._details(entry))
        return dict(entry)


    # -- workspace-managed schedules -----------------------------------------------------

    @staticmethod
    def _normalised(definition: Any) -> dict[str, Any]:
        """Validate one desired-state definition and return the fields a stored entry holds (no I/O)."""
        if not isinstance(definition, Mapping):
            raise ScheduleError("a schedule definition is an object")
        allowed = {"name", "owner", "agent", "goal", "cron", "event", "budget", "requiredCapabilities", "maxPerHour"}
        extra = sorted(set(definition) - allowed)
        if extra:
            raise ScheduleError("a schedule definition has unsupported keys: " + ", ".join(extra))
        name = definition.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise ScheduleError("a managed schedule needs a name: lowercase letters, digits and dashes")
        owner = _principal(definition.get("owner"), "owner")
        agent = definition.get("agent")
        if not isinstance(agent, str) or not _AGENT.match(agent):
            raise ScheduleError("agent must be a short Agent name")
        goal = definition.get("goal")
        if not isinstance(goal, str) or not goal.strip() or len(goal.strip()) > MAX_GOAL:
            raise ScheduleError(f"goal must be a non-empty string of at most {MAX_GOAL} characters")
        if (definition.get("cron") is None) == (definition.get("event") is None):
            raise ScheduleError("a schedule has exactly one of cron or event")
        fields: dict[str, Any] = {"name": name, "owner": owner, "agent": agent, "goal": goal.strip(), "budget": _budget(definition.get("budget"))}
        try:
            fields["requiredCapabilities"] = sorted(_delegation()._tools(definition.get("requiredCapabilities") or (), "required capabilities"))
        except PermissionError as exc:
            raise ScheduleError(str(exc)) from exc
        if definition.get("cron") is not None:
            cron = definition["cron"]
            if not isinstance(cron, str):
                raise ScheduleError("cron must be a string")
            parse_cron(cron)
            _check_interval(cron)
            fields["cron"] = " ".join(cron.split())
        else:
            if definition["event"] not in EVENT_KINDS:
                raise ScheduleError("event must be one of " + ", ".join(EVENT_KINDS))
            per_hour = definition.get("maxPerHour", DEFAULT_TRIGGER_PER_HOUR)
            if not isinstance(per_hour, int) or isinstance(per_hour, bool) or not 1 <= per_hour <= MAX_TRIGGER_PER_HOUR:
                raise ScheduleError(f"maxPerHour must be an integer from 1 to {MAX_TRIGGER_PER_HOUR}")
            fields.update(event=definition["event"], maxPerHour=per_hour)
        return fields

    def plan(self, definitions: Iterable[Mapping[str, Any]], *, now: int | None = None) -> dict[str, list[str]]:
        """What :meth:`reconcile` would do with ``definitions``, with nothing written, ledgered or changed."""
        return self.reconcile(definitions, by="plan:dry-run", now=now, dry_run=True)

    def reconcile(
        self, definitions: Iterable[Mapping[str, Any]], *, by: str, now: int | None = None, dry_run: bool = False
    ) -> dict[str, list[str]]:
        """Make the managed schedules match ``definitions``; nothing is applied unless every definition is valid.

        With ``dry_run`` the same checks run against a copy and the report says what would happen; the store is not touched.
        """
        who = _principal(by, "by")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        wanted: dict[str, dict[str, Any]] = {}
        for definition in definitions:
            fields = self._normalised(definition)
            if fields["name"] in wanted:
                raise ScheduleError(f"the name {fields['name']} is listed twice")
            if fields.get("cron") and next_fire(fields["cron"], stamp) is None:
                raise ScheduleError("this cron expression never fires")
            wanted[fields["name"]] = fields
        report: dict[str, list[str]] = {"created": [], "updated": [], "retired": [], "unchanged": []}
        changes: list[tuple[str, dict[str, Any]]] = []
        compared = ("owner", "agent", "goal", "budget", "requiredCapabilities", "cron", "event", "maxPerHour")
        with self._locked() as state:
            schedules = copy.deepcopy(state["schedules"]) if dry_run else state["schedules"]
            managed = {item["managedBy"]: item for item in schedules.values() if item.get("managedBy")}
            for name, fields in sorted(wanted.items()):
                entry = managed.get(name)
                if entry is None:
                    if len(schedules) >= MAX_SCHEDULES:
                        raise ScheduleError("the tenant has the maximum number of schedules")
                    if sum(1 for item in schedules.values() if item["owner"] == fields["owner"]) >= MAX_PER_OWNER:
                        raise ScheduleError(f"an owner may have at most {MAX_PER_OWNER} schedules")
                    entry = {
                        "id": "s-" + secrets.token_hex(6), "tenant": self.tenant, "owner": fields["owner"], "agent": fields["agent"],
                        "goal": fields["goal"], "goalDigest": goal_digest(fields["goal"]), "cron": fields.get("cron"),
                        "budget": fields["budget"], "requiredCapabilities": fields["requiredCapabilities"], "state": "active",
                        "createdAt": stamp, "runs": 0, "managedBy": name,
                        "nextFire": next_fire(fields["cron"], stamp) if fields.get("cron") else None,
                    }
                    if fields.get("event"):
                        entry.update(kind="trigger", event=fields["event"], maxPerHour=fields["maxPerHour"], recentFires=[], seenEvents=[])
                    schedules[entry["id"]] = entry
                    report["created"].append(name)
                    changes.append(("schedule.create", dict(entry)))
                    continue
                if (entry.get("kind") == "trigger") != bool(fields.get("event")):
                    raise ScheduleError(f"{name} cannot change between a timed schedule and an event trigger; use a new name")
                before = {key: entry.get(key) for key in compared}
                after = {key: fields.get(key) for key in compared}
                reactivate = entry.get("pausedReason") == "removed-from-workspace"
                if before == after and not reactivate:
                    report["unchanged"].append(name)
                    continue
                entry.update({key: fields[key] for key in ("owner", "agent", "goal", "budget", "requiredCapabilities") if key in fields})
                entry["goalDigest"] = goal_digest(entry["goal"])
                if fields.get("cron"):
                    entry["cron"] = fields["cron"]
                    if before["cron"] != fields["cron"] or reactivate:
                        entry["nextFire"] = next_fire(fields["cron"], stamp)
                if fields.get("event"):
                    entry["event"], entry["maxPerHour"] = fields["event"], fields["maxPerHour"]
                if reactivate:
                    entry["state"] = "active"
                    entry.pop("pausedReason", None)
                    entry["consecutiveFailures"] = 0
                report["updated"].append(name)
                changes.append(("schedule.update", dict(entry)))
            for name, entry in sorted(managed.items()):
                if name not in wanted and entry["state"] == "active":
                    entry["state"], entry["pausedReason"] = "paused", "removed-from-workspace"
                    report["retired"].append(name)
                    changes.append(("schedule.retire", dict(entry)))
        if not dry_run:
            for action, entry in changes:
                self._record(action, who, {**self._details(entry), "managedBy": entry.get("managedBy")})
        return report

    def export_desired(self) -> list[dict[str, Any]]:
        """The managed schedules as definitions ``reconcile`` accepts (sorted by name); unmanaged schedules are not exported."""
        with self._locked() as state:
            managed = [dict(item) for item in state["schedules"].values() if item.get("managedBy")]
        result = []
        for entry in sorted(managed, key=lambda item: item["managedBy"]):
            definition: dict[str, Any] = {
                "name": entry["managedBy"], "owner": entry["owner"], "agent": entry["agent"], "goal": entry["goal"],
                "budget": dict(entry["budget"]), "requiredCapabilities": list(entry.get("requiredCapabilities", [])),
            }
            if entry.get("kind") == "trigger":
                definition.update(event=entry["event"], maxPerHour=entry["maxPerHour"])
            else:
                definition["cron"] = entry["cron"]
            result.append(definition)
        return result

    # -- firing -------------------------------------------------------------------------

    def claim_due(self, *, now: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Atomically take every active cron schedule whose slot has arrived (at most ``limit``), moving each to its next slot.

        Unlike ``due`` followed by ``mark_fired``, two processes calling this together can never both take the same slot.
        Each returned entry already carries its new run number in ``runs``.
        """
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        claimed: list[dict[str, Any]] = []
        with self._locked() as state:
            ready = sorted(
                (item for item in state["schedules"].values()
                 if item["state"] == "active" and item.get("cron") and item.get("nextFire") is not None and item["nextFire"] <= stamp),
                key=lambda item: (item["nextFire"], item["id"]),
            )
            for entry in ready[:limit]:
                entry["runs"] = int(entry.get("runs", 0)) + 1
                entry["lastFiredAt"] = stamp
                entry["nextFire"] = next_fire(entry["cron"], stamp)
                claimed.append(dict(entry))
        for entry in claimed:
            self._record("schedule.fire", f"schedule:{entry['id']}", {**self._details(entry), "run": entry["runs"]})
        return claimed

    def claim_event(self, event: str, event_ref: str, *, now: int | None = None) -> dict[str, list[dict[str, Any]]]:
        """Atomically take the active triggers for ``event``: ``{"claimed": [...], "limited": [...], "duplicate": [...]}``.

        ``event_ref`` is an opaque identifier of the event (a message id); only its digest is kept. A trigger that already
        handled this event is ``duplicate``; one that has used its per-hour allowance is ``limited`` (the event is not
        queued: it is dropped for that trigger and the caller is told).
        """
        if event not in EVENT_KINDS:
            raise ScheduleError("event must be one of " + ", ".join(EVENT_KINDS))
        if not isinstance(event_ref, str) or not event_ref or len(event_ref) > 512:
            raise ScheduleError("an event needs a reference of at most 512 characters")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        digest = hashlib.sha256(event_ref.encode("utf-8")).hexdigest()
        result: dict[str, list[dict[str, Any]]] = {"claimed": [], "limited": [], "duplicate": []}
        with self._locked() as state:
            for entry in sorted(state["schedules"].values(), key=lambda item: (item["createdAt"], item["id"])):
                if entry.get("kind") != "trigger" or entry["state"] != "active" or entry.get("event") != event:
                    continue
                if digest in entry.get("seenEvents", []):
                    result["duplicate"].append(dict(entry))
                    continue
                recent = [moment for moment in entry.get("recentFires", []) if moment > stamp - 3600]
                entry["recentFires"] = recent
                entry["seenEvents"] = [*entry.get("seenEvents", []), digest][-MAX_SEEN_EVENTS:]
                if len(recent) >= entry["maxPerHour"]:
                    result["limited"].append(dict(entry))
                    continue
                entry["recentFires"] = [*recent, stamp]
                entry["runs"] = int(entry.get("runs", 0)) + 1
                entry["lastFiredAt"] = stamp
                result["claimed"].append(dict(entry))
        for entry in result["claimed"]:
            self._record("schedule.fire", f"schedule:{entry['id']}", {**self._details(entry), "run": entry["runs"], "eventDigest": digest})
        for entry in result["limited"]:
            self._record("schedule.rate-limited", f"schedule:{entry['id']}", {**self._details(entry), "eventDigest": digest})
        return result

    def mark_outcome(self, schedule_id: str, *, ok: bool, now: int | None = None) -> dict[str, Any]:
        """Record how a run ended. Three failures in a row suspend the schedule (``pausedReason: repeated-failures``)."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        suspended = False
        with self._locked() as state:
            entry = state["schedules"].get(schedule_id)
            if entry is None:
                raise ScheduleError("no such schedule")
            entry["lastOutcome"], entry["lastOutcomeAt"] = ("ok" if ok else "failed"), stamp
            entry["consecutiveFailures"] = 0 if ok else int(entry.get("consecutiveFailures", 0)) + 1
            if not ok and entry["consecutiveFailures"] >= MAX_FAILURES and entry["state"] == "active":
                entry["state"], entry["pausedReason"] = "paused", "repeated-failures"
                suspended = True
            result = dict(entry)
        if suspended:
            self._record("schedule.suspend", f"schedule:{schedule_id}", {**self._details(result), "reason": "repeated-failures"})
        return result

    # -- run records ---------------------------------------------------------------------

    def start_run(self, task_id: str, schedule: Mapping[str, Any], *, now: int, trigger: str) -> dict[str, Any]:
        """Remember one run (state ``running``). The record holds identifiers, states and counts only; never the goal."""
        record = {
            "taskId": task_id, "scheduleId": schedule["id"], "agent": schedule["agent"], "owner": schedule["owner"],
            "trigger": trigger, "state": "running", "startedAt": now,
        }
        with self._locked() as state:
            runs = state.setdefault("runs", {})
            runs[task_id] = record
            if len(runs) > MAX_RUNS:
                finished = sorted((item for item in runs.values() if item["state"] not in ("running", "waiting")), key=lambda item: item["startedAt"])
                for old in finished[: len(runs) - MAX_RUNS]:
                    runs.pop(old["taskId"], None)
        return dict(record)

    def update_run(self, task_id: str, **fields: Any) -> dict[str, Any]:
        with self._locked() as state:
            record = state.setdefault("runs", {}).get(task_id)
            if record is None:
                raise ScheduleError("no such run")
            record.update(fields)
            return dict(record)

    def runs(self, *, states: tuple[str, ...] | None = None, schedule_id: str | None = None) -> list[dict[str, Any]]:
        with self._locked() as state:
            records = [dict(item) for item in state.get("runs", {}).values()]
        if states is not None:
            records = [item for item in records if item["state"] in states]
        if schedule_id is not None:
            records = [item for item in records if item["scheduleId"] == schedule_id]
        return sorted(records, key=lambda item: (item["startedAt"], item["taskId"]))

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        with self._locked() as state:
            entry = state["schedules"].get(schedule_id)
            return dict(entry) if entry is not None else None

    def due(self, *, now: int | None = None) -> list[dict[str, Any]]:
        """Active schedules whose slot has arrived. Missed slots collapse into one firing."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        return [item for item in self.list() if item["state"] == "active" and item.get("nextFire") is not None and item["nextFire"] <= stamp]

    def mark_fired(self, schedule_id: str, *, now: int | None = None) -> dict[str, Any]:
        """Record that a run started and move to the next slot after now (never re-fires the same slot)."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        with self._locked() as state:
            entry = state["schedules"].get(schedule_id)
            if entry is None:
                raise ScheduleError("no such schedule")
            if entry["state"] != "active":
                raise ScheduleError("a paused schedule does not fire")
            entry["runs"] = int(entry.get("runs", 0)) + 1
            entry["lastFiredAt"] = stamp
            entry["nextFire"] = next_fire(entry["cron"], stamp)
            result = dict(entry)
        self._record("schedule.fire", f"schedule:{schedule_id}", {**self._details(result), "run": result["runs"]})
        return result


def run_envelope(
    schedule: Mapping[str, Any],
    *,
    agent_grants: Iterable[str],
    owner_permissions: Iterable[str],
    run_number: int | None = None,
    deadline: int | None = None,
) -> dict[str, Any]:
    """The task envelope for one firing: the owner's permissions narrowed to the Agent's grants, with the schedule's budget.

    Raises ``ScheduleError`` when the schedule needs a Tool the owner (now) or the Agent does not hold; the run
    must then not start, and the owner should be told (a runtime concern, not done here).
    """
    runs = int(schedule.get("runs", 0)) if run_number is None else run_number
    try:
        return _delegation().root_envelope(
            task_id=f"{schedule['id']}:run-{runs}", agent=schedule["agent"], originating_principal=schedule["owner"],
            goal=schedule["goal"], agent_grants=agent_grants, originator_permissions=owner_permissions,
            budget=schedule["budget"], deadline=deadline, required_capabilities=schedule.get("requiredCapabilities", []),
        )
    except PermissionError as exc:  # delegation.DelegationError
        raise ScheduleError(str(exc)) from exc
