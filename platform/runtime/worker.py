"""The governed runtime worker: the clock that runs schedules, drains the inbound mailbox and tells people what needs them.

The stores and runners already exist and were deliberately not wired to a clock (#99, #109, #86, #88, #97, #80). This service is
that clock. It runs beside the governed sidecar on the same governance volume, as its own Compose service, and every pass does a
small, bounded amount of work:

- **schedules** (once at start): the workspace's ``Schedule`` definitions carried in the compiled desired state are reconciled
  into the schedule store under the deployer's name, so a deploy applies them without an operator command. A managed schedule
  that is no longer listed is paused, never deleted; schedules created from chat are untouched.
- **scheduler**: runs due schedules and resumes tasks whose human step was answered, with the coworker (through the Hermes API
  server, ``coworker_client.py``) as the runner. Authority is the Agent's compiled Tools narrowed to the owner's permissions
  from the worker configuration; an owner with no entry has none, so the run is refused and the owner told.
- **mailbox**: drains the built-in inbound spool into the support mailbox handler. The coworker classifies each message and
  writes an answer only from approved knowledge found by a governed ``kb.search`` (ledgered like any other read); a reply is
  kept as a Tier 0 draft in the artifact store and the mailbox owner is told it is ready, so nothing is sent without a person.
- **notices**: delivers hand-off, human-step, connection, grant, schedule and mailbox notices to the named person through the
  channel the people directory gives for them (``notice_delivery.py``). A person with no usable channel stays ``no-channel`` and
  is offered again on the next pass; nothing is marked delivered early.
- **crm-sync**: files each handled customer email to the bound Tier 0 contact log (against the one contact holding a verified sender's address, else the unmatched queue) through the governed ``crm.log-interaction`` boundary
  (the support mailbox does the first attempt), retries the ones a transient outage left behind when their backoff has passed, and tells
  the mailbox owner once about any filing that is blocked and needs a person (``client-operator.py crm-sync``). Nothing is reported as
  filed unless the CRM acknowledged it.
- **events**: the event sources the worker does not witness itself. It fires ``chat.message`` for each digest the channel gateway left
  in the shared event outbox (``event_outbox.py``; no sender, id or text) and ``ticket.created`` for each Tier 0 ticket created since
  its last pass by any route (the coworker through the sidecar, a person); a ticket the mailbox already announced is not run twice.
- **knowledge**: tells the escalation contact (else the mailbox owner) once when an approved business-rules source is due for review
  within two weeks and once when it has passed its ``reviewBy`` date (after which the coworker stops answering from it), naming only
  the source and the date.
- **calls** (when the voice container's transcript volume is mounted, ``MODEL_GARDEN_VOICE_TRANSCRIPTS``): files each finished phone call
  to the bound contact log through the governed ``crm.log-interaction`` boundary as ``runtime:conversation-filing`` (always the
  ``unmatched`` queue: a caller's number is not a verified identity), after a post-call summary written by the coworker from the
  already-redacted turns (else a deterministic fallback), puts the call on the cross-channel timeline with the ledger request ids of
  the Tool calls the coworker made during it (``call_requests.py``, attributed by time and labelled so), and ledgers each
  ``TranscriptWriteFailed`` once. The volume is read-only here; the worker never writes to the transcript store.
- **retention**: purges expired artifacts and mail past the retention period.
- **usage**: meters model usage from the Hermes session stores mounted read-only (``MODEL_GARDEN_USAGE_SOURCES``) and ledgers
  what changed since the last pass as ``usage.recorded`` (``platform/metering/usage.py``): tokens, API calls and cost per session,
  channel and model, counts only. The export carries them to the bucket, where the billing sync reads them for Lago.
- **export** (when configured): copies new ledger records to the retention-locked bucket; a local ledger that no longer matches
  the export, or one falling behind because exporting fails, is alerted to ``escalateTo`` through the notices job
  (``export_monitor.py``): once when it starts, a reminder while it lasts, and once when it recovers.

A failing job is ledgered as ``worker.job-failed`` with the job name and error type only and never stops the other jobs. A
heartbeat file records the last pass for the container health check. The worker holds no provider credential and no model
credential; the only secret it reads is the Hermes API server key (``MODEL_GARDEN_COWORKER_API_KEY``), and only when a coworker
URL is configured.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_INTERVALS = {"scheduler": 60, "mailbox": 30, "notices": 60, "retention": 3600, "usage": 300, "export": 300, "crm-sync": 120, "calls": 120, "changes": 60, "events": 30, "knowledge": 21_600}
MIN_INTERVAL, MAX_INTERVAL = 10, 86_400
# A stalled export alerts the escalation contact when the ledger is this far ahead of it, or has waited this long.
EXPORT_MAX_LAG_EVENTS = 100
EXPORT_MAX_STALE_SECONDS = 6 * 3600
MAX_QUEUE = 2_000
MAX_EVENT_BATCH = 50
IDENTITY = "runtime:worker"
CALL_FILING_IDENTITY = "runtime:conversation-filing"


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


MODULES = {
    "govern": "scripts/govern-action.py",
    "schedules": "platform/core/schedules.py",
    "scheduler": "platform/core/scheduler.py",
    "delivery": "platform/core/notice_delivery.py",
    "notices": "platform/core/connection_notices.py",
    "capabilities": "platform/core/capabilities.py",
    "grants": "platform/core/provider_grants.py",
    "artifacts": "platform/core/artifacts.py",
    "spool": "platform/channels/mail_spool.py",
    "recorder": "platform/channels/conversation_recorder.py",
    "support": "platform/channels/support_mailbox.py",
    "email": "platform/channels/email_channel.py",
    "text": "platform/channels/text_channel.py",
    "coworker": "platform/runtime/coworker_client.py",
    "sidecar": "platform/runtime/governed_mcp.py",
    "transports": "platform/channels/transports.py",
    "ledger": "platform/core/ledger_export.py",
    "export_monitor": "platform/core/export_monitor.py",
    "usage": "platform/metering/usage.py",
    "approval": "scripts/runtime-approval.py",
    "crm_sync": "platform/core/crm_sync.py",
    "outbox": "platform/core/event_outbox.py",
    "knowledge": "platform/core/knowledge.py",
    "crm_filing": "platform/core/crm_filing.py",
    "audit_query": "platform/core/audit_query.py",
    "filing": "platform/channels/conversation_filing.py",
    "call_summary": "platform/channels/call_summary.py",
    "call_requests": "platform/channels/call_requests.py",
    "studio_overlay": "platform/core/studio_overlay.py",
}


def _module(key: str):
    return _load(f"model_garden_worker_{key}", MODULES[key])


class WorkerConfigError(ValueError):
    """The worker configuration is unusable; the message names the field, never a secret."""


def tenant_of(desired_state: Mapping[str, Any]) -> str:
    client = desired_state.get("source", {}).get("client") if isinstance(desired_state.get("source"), Mapping) else None
    return re.sub(r"[^a-z0-9-]+", "-", str(client or "").lower()).strip("-")[:63] or "default"


def _person(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}", value):
        raise WorkerConfigError(f"{label} must be a named person")
    return value


def load_config(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the non-secret worker configuration rendered from ``EnvironmentBinding.spec.worker``."""
    if not isinstance(document, Mapping):
        raise WorkerConfigError("worker configuration must be an object")
    allowed = {"deployedBy", "intervals", "people", "escalateTo", "owners", "mailbox", "coworker", "connectors", "artifactRetentionDays", "export"}
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise WorkerConfigError(f"unknown worker settings: {', '.join(unknown)}")
    intervals = dict(DEFAULT_INTERVALS)
    for name, value in (document.get("intervals") or {}).items():
        if name not in DEFAULT_INTERVALS or not isinstance(value, int) or isinstance(value, bool) or not MIN_INTERVAL <= value <= MAX_INTERVAL:
            raise WorkerConfigError(f"intervals.{name} must be a job name with {MIN_INTERVAL} to {MAX_INTERVAL} seconds")
        intervals[name] = value
    owners: dict[str, list[str]] = {}
    for person, tools in (document.get("owners") or {}).items():
        _person(person, "an owner")
        if not isinstance(tools, list) or not all(isinstance(tool, str) and re.fullmatch(r"\*|[a-z][a-z0-9-]*\.[a-z][a-z0-9.-]*", tool) for tool in tools):
            raise WorkerConfigError(f"owners.{person} must list Tool names or '*'")
        owners[person] = list(tools)
    mailbox = document.get("mailbox")
    if mailbox is not None:
        if not isinstance(mailbox, Mapping) or not mailbox.get("addresses") or not mailbox.get("trustedAuthservId"):
            raise WorkerConfigError("mailbox needs addresses and trustedAuthservId")
        _person(mailbox.get("owner"), "mailbox.owner")
    coworker = document.get("coworker")
    if coworker is not None:
        if not isinstance(coworker, Mapping) or set(coworker) - {"url", "model"}:
            raise WorkerConfigError("coworker holds only url and model; the key comes from MODEL_GARDEN_COWORKER_API_KEY")
        _module("coworker").check_base_url(str(coworker.get("url", "")))
    export = document.get("export")
    if export is not None:
        if not isinstance(export, Mapping) or set(export) - {"gcsBucket", "impersonate", "prefix"} or not export.get("gcsBucket"):
            raise WorkerConfigError("export holds gcsBucket plus optional impersonate and prefix")
        _module("ledger").GcsSink(str(export["gcsBucket"]), lambda: "", prefix=str(export.get("prefix") or ""))  # validates the names
        if export.get("impersonate") is not None:
            _module("ledger").impersonated_token(str(export["impersonate"]), lambda: "", scope="x")
    escalate_to = document.get("escalateTo")
    if escalate_to is not None:
        _person(escalate_to, "escalateTo")
    people = document.get("people") or {}
    _module("delivery").Directory(people)  # validates: no credentials, one channel each
    connectors = document.get("connectors") or {}
    if not isinstance(connectors, Mapping):
        raise WorkerConfigError("connectors must be the binding's connector map")
    return {
        "deployedBy": document.get("deployedBy") or None, "intervals": intervals, "people": dict(people), "escalateTo": escalate_to,
        "owners": owners, "mailbox": dict(mailbox) if mailbox else None, "coworker": dict(coworker) if coworker else None,
        "connectors": dict(connectors), "artifactRetentionDays": document.get("artifactRetentionDays"),
        "export": dict(export) if export else None,
    }


class NoticeQueue:
    """Notices the worker itself produces (schedule outcomes, mailbox threads), delivered like hand-off notices."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / "worker-notices.json"

    @contextlib.contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.root / ".worker-notices.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {"pending": {}}
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                temporary = self.path.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(state, sort_keys=True) + "\n")
                os.replace(temporary, self.path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def add(self, kind: str, to: str | None, text: str, *, reference: str) -> str:
        key = hashlib.sha256(f"{kind}|{to}|{reference}".encode()).hexdigest()[:24]
        with self._locked() as state:
            if key not in state["pending"] and len(state["pending"]) < MAX_QUEUE:
                state["pending"][key] = {"key": key, "kind": kind, "to": to, "text": text[:600], "reference": reference[:40]}
        return key

    def source(self):
        with self._locked() as state:
            notices = list(state["pending"].values())

        def mark(item: Mapping[str, Any], via: str) -> None:
            with self._locked() as state:
                state["pending"].pop(item["notice"]["key"], None)

        return _module("delivery").source("worker", notices, mark, reference=lambda notice: f"{notice['reference']}:{notice['kind']}")

    def pending(self) -> list[dict[str, Any]]:
        with self._locked() as state:
            return list(state["pending"].values())


def inbox_sender(address: str, text: str) -> None:
    """The built-in inbox is a channel only when the person's entry says so; nothing is sent, the notice is listed there."""
    return None


class Worker:
    def __init__(
        self,
        desired_state_path: pathlib.Path,
        data_root: pathlib.Path,
        config: Mapping[str, Any],
        *,
        coworker: Any | None = None,
        senders: Mapping[str, Callable[[str, str], Any]] | None = None,
        clock: Callable[[], float] = time.time,
        export_sink: Any | None = None,
        usage_sources: Mapping[str, pathlib.Path] | None = None,
        voice_transcripts: pathlib.Path | None = None,
    ) -> None:
        self.desired_state_path = pathlib.Path(desired_state_path)
        self.desired_state = json.loads(self.desired_state_path.read_text(encoding="utf-8"))
        self.data = pathlib.Path(data_root)
        self.audit_path = self.data / "audit.jsonl"
        self.config = load_config(config)
        self.tenant = tenant_of(self.desired_state)
        self.coworker = coworker
        self.senders = dict(senders or {})
        self.clock = clock
        self.export_sink = export_sink
        self.usage_sources = dict(usage_sources or {})
        self.voice_transcripts = pathlib.Path(voice_transcripts) if voice_transcripts else None
        self.queue = NoticeQueue(self.data / "worker")
        self.last_run: dict[str, float] = {}
        self.heartbeat = self.data / "worker" / "heartbeat.json"
        self._failure_streaks: dict[str, int] = {}  # per-job attempts, not every loop tick
        self._schedules_applied = False

    # -- shared ------------------------------------------------------------------------------------

    def audit(self, action: str, by: str, details: dict[str, Any]) -> None:
        _module("govern").record_operator_action(self.audit_path, action, by, details)

    def _agent(self) -> str:
        return str(self.desired_state.get("agent", {}).get("metadata", {}).get("name") or self.desired_state.get("source", {}).get("name"))

    def _agent_tools(self) -> list[str]:
        return sorted(str(tool.get("metadata", {}).get("name")) for tool in self.desired_state.get("tools", []) if isinstance(tool, Mapping))

    def grants_for(self, agent: str) -> list[str]:
        return self._agent_tools() if agent == self._agent() else []

    def permissions_for(self, owner: str) -> list[str]:
        tools = self.config["owners"].get(owner, [])
        return self._agent_tools() if "*" in tools else [tool for tool in tools if tool in self._agent_tools()]

    def _schedule_store(self):
        return _module("schedules").ScheduleStore(self.data / "schedules", tenant=self.tenant, audit=self.audit)

    # -- jobs ----------------------------------------------------------------------------------------

    def apply_schedules(self, now: int) -> dict[str, Any]:
        definitions = self.desired_state.get("schedules") or []
        if not definitions:
            return {"applied": False, "reason": "no-workspace-schedules"}
        by = self.config["deployedBy"]
        if not by:
            return {"applied": False, "reason": "no-deployer-named"}
        store = self._schedule_store()
        report = store.reconcile(self._with_studio_edits(store, definitions), by=by, now=now)
        return {"applied": True, **{key: len(value) for key, value in report.items()}}

    def _studio_schedule_edits(self) -> dict[str, Mapping[str, Any]]:
        edits = _module("studio_overlay").read(self.data).get("schedules")
        return {key: value for key, value in edits.items() if isinstance(key, str) and isinstance(value, Mapping)} if isinstance(edits, Mapping) else {}

    def _with_studio_edits(self, store: Any, definitions: Any) -> list[Any]:
        """The workspace's schedule definitions with the owner's approved cron/budget edits, so a reconcile does not undo them."""
        edits = self._studio_schedule_edits()
        if not edits:
            return list(definitions)
        by_name = {entry.get("managedBy"): entry["id"] for entry in store.list() if entry.get("managedBy")}
        merged = []
        for definition in definitions:
            edit = edits.get(by_name.get(definition.get("name") if isinstance(definition, Mapping) else None), {})
            if edit and isinstance(definition, Mapping) and definition.get("cron") is not None:
                definition = {**definition, **{key: edit[key] for key in ("cron", "budget") if key in edit}}
            merged.append(definition)
        return merged

    def run_changes(self, now: int) -> dict[str, Any]:
        """Apply the schedule edits the owner approved in the Studio to the live schedules (``studio_overlay.py`` for what applies live)."""
        edits = self._studio_schedule_edits()
        if not edits:
            return {"edits": 0}
        store = self._schedule_store()
        applied = skipped = 0
        for schedule_id, edit in sorted(edits.items()):
            try:
                changed = store.edit(schedule_id, by=IDENTITY, cron=edit.get("cron"), budget=edit.get("budget"), now=now)
            except _module("schedules").ScheduleError:
                skipped += 1  # a schedule that is gone, or values the store refuses, stay in the overlay and the workspace patch
                continue
            applied += 1 if changed else 0
        return {"edits": len(edits), "applied": applied, "notApplied": skipped}

    def _scheduler(self):
        return _module("scheduler").Scheduler(
            self._schedule_store(), _module("coworker").schedule_runner(self.coworker),
            grants_for=self.grants_for, permissions_for=self.permissions_for, handoff_root=self.data / "handoffs",
            audit=self.audit,
            notify=lambda notice: self.queue.add(notice["kind"], notice.get("to"), notice["text"], reference=str(notice.get("taskId", ""))[:40]),
        )

    def run_scheduler(self, now: int) -> dict[str, Any]:
        if self.coworker is None:
            return {"skipped": "no-coworker-configured"}
        scheduler = self._scheduler()
        due = scheduler.run_due(now=now)
        resumed = scheduler.resume_waiting(now=now)
        return {"runs": len(due["runs"]), "resumed": len(resumed["runs"])}

    def fire_event(self, event: str, reference: str, now: int) -> dict[str, Any]:
        """Run the active triggers for one event the worker witnessed (``mail.received``, ``ticket.created``), once per event reference.

        Only the event kind and a digest of the reference reach the runner (``scheduler.run_event``); a trigger that already ran for this
        event, or is at its hourly limit, is skipped. A failure is ledgered by type and never stops the work that produced the event.
        """
        if self.coworker is None:
            return {"skipped": "no-coworker-configured"}
        try:
            outcome = self._scheduler().run_event(event, reference, now=now)
        except Exception as exc:  # noqa: BLE001 - the mail or ticket is already handled; say only what failed
            self.audit("worker.event-failed", IDENTITY, {"event": event, "error": type(exc).__name__})
            return {"failed": type(exc).__name__}
        return {"runs": len(outcome["runs"]), "limited": len(outcome["limited"]), "duplicate": len(outcome["duplicate"])}

    def _events_state_path(self) -> pathlib.Path:
        return self.data / "worker" / "events.json"

    def _events_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._events_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        offset, since = state.get("offset"), state.get("ticketsSince")
        at_since = state.get("ticketsAtSince")
        return {
            "offset": offset if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0 else 0,
            "ticketsSince": since if isinstance(since, int) and not isinstance(since, bool) else None,
            "ticketsAtSince": [item for item in at_since if isinstance(item, str)][:200] if isinstance(at_since, list) else [],
        }

    def _save_events_state(self, state: Mapping[str, Any]) -> None:
        path = self._events_state_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def run_events(self, now: int) -> dict[str, Any]:
        """Fire ``chat.message`` for what the gateway reported and ``ticket.created`` for tickets nobody announced; once per event."""
        if self.coworker is None:
            return {"skipped": "no-coworker-configured"}  # leave the events where they are until there is something to run them
        state = self._events_state()
        outbox = _module("outbox").EventOutbox(self.data / "events")
        chat, tickets_fired = 0, 0
        events, offset = outbox.read(state["offset"])
        for event in events:
            self.fire_event("chat.message", str(event["ref"]), now)
            chat += 1
        state["offset"] = outbox.compact(offset)
        try:
            store = self._sidecar().tickets_store()
        except PermissionError:  # no built-in ticket store is bound
            store = None
        if store is not None:
            if state["ticketsSince"] is None:
                state["ticketsSince"], state["ticketsAtSince"] = now, []  # start now: tickets from before this worker watched are not replayed
            else:
                fresh = [item for item in store.created_since(state["ticketsSince"]) if item["ticketId"] not in state["ticketsAtSince"]][:MAX_EVENT_BATCH]
                for item in fresh:
                    self.fire_event("ticket.created", item["ticketId"], now)  # the mailbox path may have announced it already: the store skips a repeat
                    tickets_fired += 1
                if fresh:
                    newest = fresh[-1]["createdAt"]
                    carried = state["ticketsAtSince"] if newest == state["ticketsSince"] else []
                    state["ticketsSince"] = newest
                    state["ticketsAtSince"] = [*carried, *(item["ticketId"] for item in fresh if item["createdAt"] == newest)]
        self._save_events_state(state)
        return {"chat": chat, "tickets": tickets_fired}

    def _sidecar(self, identity: str = "runtime:support-mailbox"):
        environ = {
            **os.environ, "MODEL_GARDEN_INITIATING_IDENTITY": identity,
            "MODEL_GARDEN_CAPABILITY_MANIFEST": os.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST", ""),
        }
        return _module("sidecar").GovernedToolRuntime(
            self.desired_state_path, self.audit_path, self.data / "approvals", environ=environ,
        )

    def _artifacts(self):
        days = self.config.get("artifactRetentionDays")
        options = {"retention_days": days} if isinstance(days, int) else {}
        return _module("artifacts").ArtifactStore(self.data / "artifacts", tenant=self.tenant, audit=self.audit, **options)

    def _conversation_recorder(self):
        """The cross-channel timeline recorder; customers link only on verified evidence, and only against the Tier 0 contact log."""
        recorder = _module("recorder")
        store = recorder._conversations().ConversationStore(self.data / "conversations", tenant=self.tenant, audit=self.audit)
        try:
            contacts = self._sidecar().crm_store()
        except PermissionError:  # no Tier 0 contact log is bound: conversations are recorded and never linked
            return recorder.ConversationRecorder(store, agent=self._agent())
        # Rebuilt on each lookup so a contact a person adds while the worker runs can be matched by the next email.
        return recorder.ConversationRecorder(store, agent=self._agent(), directory=lambda kind, digest: recorder.contacts_directory(contacts)(kind, digest))

    # -- CRM filing --------------------------------------------------------------------------------

    def _crm_filing(self):
        """``(sync ledger, payload store, executor)`` for filing handled emails to the Tier 0 contact log, or ``None`` when `crm` is not bound to it.

        The executor is the governed ``crm.log-interaction`` boundary (policy, approval, ledger); a result that is not ``executed`` (an
        approval is pending, a person has it, it was denied) is raised as ``PermissionError`` so the entry blocks for a person instead of
        being reported as filed.
        """
        sidecar = self._sidecar()
        try:
            sidecar.crm_store()
        except PermissionError:
            return None
        sync, filing = _module("crm_sync"), _module("crm_filing")
        ledger = sync.SyncLedger(self.data / "crm-sync", tenant=self.tenant, audit=self.audit)
        store = filing.PayloadStore(self.data / "crm-sync" / "payloads")

        def executor(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            result = sidecar.crm_log_interaction(
                object_id=arguments["objectId"], interaction_type=arguments["interactionType"], channel=arguments["channel"],
                direction=arguments["direction"], timestamp=arguments["timestamp"], summary=arguments["summary"],
                outcome=arguments.get("outcome"), transcript_ref=arguments.get("transcriptRef"),
                justification="support mailbox: file a handled customer email",
            )
            if result.get("status") != "executed":
                raise PermissionError(f"filing was not allowed to run ({result.get('status')})")
            return result["result"]

        return ledger, store, executor

    def _contact_for(self) -> Callable[[str], str]:
        """Which contact a *verified* sender's email is filed against: the one contact holding that address, else the ``unmatched`` queue.

        The mailbox only offers an address the receiving server verified. No holder, several holders (a possible duplicate a person has
        to settle) and any store problem all mean ``unmatched``; a person assigns it (`client-operator.py contacts review|assign`).
        """
        sidecar = self._sidecar()

        def contact_for(address: str) -> str:
            try:
                holders = sidecar.crm_store().find_by_email(address)
            except (PermissionError, OSError, ValueError):
                return "unmatched"
            return holders[0] if len(holders) == 1 else "unmatched"

        return contact_for

    def run_calls(self, now: int) -> dict[str, Any]:
        """File finished phone calls from the voice container's transcript volume (mounted read-only); see the module docstring."""
        path = self.voice_transcripts
        if path is None or not path.is_file():
            return {"skipped": "no-voice-transcripts"}
        sidecar = self._sidecar(CALL_FILING_IDENTITY)
        try:
            store = sidecar.crm_store()
        except PermissionError:
            return {"skipped": "no-crm-bound"}
        filing = _module("filing")
        records = filing.read_call_transcripts(path)
        try:
            records = _module("call_requests").attach_request_ids(records, _module("audit_query").read_ledger(self.audit_path))
        except ValueError:  # an unreadable ledger only means the calls are filed without request ids
            pass
        summarizer = _module("call_summary").CallSummarizer(self.coworker, cache_path=self.data / "call-summaries.json")
        totals = filing.file_call_transcripts(sidecar, store, records, recorder=self._conversation_recorder(), summarizer=summarizer)
        ledgered = filing.ledger_transcript_failures(_module("govern"), self.audit_path, filing.read_transcript_failures(path), performed_by=CALL_FILING_IDENTITY)
        totals["notFiled"] = totals.pop("failed")  # a "failed" key would read as the job itself failing
        return {"records": len(records), **totals, "writeFailures": ledgered, "summaries": summarizer.counts}

    def _told_once(self, kind: str, reference: str) -> bool:
        """True the first time this ``kind`` and ``reference`` is seen and remembers it, so a condition that lasts is announced once."""
        path = self.data / "worker" / "told.json"
        digest = hashlib.sha256(f"{kind}|{reference}".encode("utf-8")).hexdigest()[:24]
        try:
            known = [item for item in json.loads(path.read_text(encoding="utf-8")) if isinstance(item, str)]
        except (OSError, ValueError, TypeError):
            known = []
        if digest in known:
            return False
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps([*known, digest][-MAX_QUEUE:]) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return True

    def run_knowledge(self, now: int) -> dict[str, Any]:
        """Tell the contact when an approved business-rules source is due for review or has passed its ``reviewBy`` date (declared dates only)."""
        to = self.config["escalateTo"] or (self.config["mailbox"] or {}).get("owner")
        if not to:
            return {"skipped": "nobody-to-tell"}
        knowledge = _module("knowledge")
        today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
        due = stale = 0
        for item in self.desired_state.get("knowledge") or []:
            spec = item.get("spec") if isinstance(item, Mapping) else None
            name = (item.get("metadata") or {}).get("name") if isinstance(item, Mapping) else None
            if not isinstance(spec, Mapping) or not isinstance(name, str) or not spec.get("reviewBy"):
                continue
            state = knowledge.source_state(spec, agent=self._agent(), today=today)
            try:
                days = (dt.date.fromisoformat(str(spec["reviewBy"])) - today).days
            except ValueError:
                continue
            if state == "stale":
                phase = "stale"
            elif state == "usable" and days <= knowledge.REVIEW_SOON_DAYS:
                phase = "due-soon"
            else:
                continue
            if not self._told_once("knowledge-review", f"{name}|{spec['reviewBy']}|{phase}"):
                continue
            if phase == "stale":
                stale += 1
                text = (f"The business-rules source {name} passed its review date ({spec['reviewBy']}), so the coworker has stopped answering from it. "
                        "Confirm the content is still right, set a later reviewBy and redeploy.")
            else:
                due += 1
                text = f"The business-rules source {name} is due for review on {spec['reviewBy']} ({days} day(s) left). After that the coworker stops answering from it."
            self.queue.add("knowledge-review", to, text, reference=f"{name}:{phase}"[:40])
        return {"dueSoon": due, "stale": stale}

    def _blocked_notified(self) -> pathlib.Path:
        return self.data / "crm-sync" / "blocked-notified.json"

    def run_crm_sync(self, now: int) -> dict[str, Any]:
        filing = self._crm_filing()
        if filing is None:
            return {"skipped": "no-built-in-crm-bound"}
        ledger, store, executor = filing
        counts = _module("crm_filing").retry_due(ledger, executor, store, now=now)
        blocked = ledger.list(states=("blocked",))
        path = self._blocked_notified()
        try:
            known = set(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            known = set()
        owner = (self.config["mailbox"] or {}).get("owner") or self.config["escalateTo"]
        fresh = [entry for entry in blocked if entry["key"] not in known]
        for entry in fresh:
            if owner:
                self.queue.add("crm-filing-blocked", owner,
                               f"A {entry['interactionType']} could not be filed to the CRM and needs a person (reason: {entry.get('lastError', 'attempts')}, "
                               f"reference {entry['key'][:12]}). See `client-operator.py crm-sync list`.", reference=entry["key"][:40])
        if fresh and owner:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(sorted(known | {entry["key"] for entry in fresh})[-MAX_QUEUE:]) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        return {**counts, "blocked": len(blocked), "notified": len(fresh) if owner else 0}

    def mailbox_handler(self) -> Callable[[bytes, int], dict[str, Any]]:
        settings = self.config["mailbox"]
        email, support, text = _module("email"), _module("support"), _module("text")
        adapter = email.EmailChannelAdapter(
            settings["trustedAuthservId"], own_addresses=tuple(settings["addresses"]),
            reply_limiter=email.ReplyLimiter(self.data / "mail-limits", int(settings.get("repliesPerThread", 3))),
        )
        sidecar, store, owner, coworker = self._sidecar(), self._artifacts(), settings["owner"], self.coworker

        def classify(message: Any) -> str:
            return coworker.classify(message.text, sorted(support.CATEGORIES), purpose="a customer support mailbox",
                                     session=f"mail:{support.thread_reference(message.thread_id or message.conversation_id)}")

        def answer(message: Any, category: str) -> Mapping[str, Any] | None:
            found = sidecar.kb_search(query=message.text[:500], limit=3, justification=f"support mailbox {category} reply")
            documents = found.get("result", {}).get("documents", []) if found.get("status") == "executed" else []
            sections = [{"source": document["citation"], "text": document["content"]} for document in documents]
            return coworker.answer(message.text, sections)

        def draft(arguments: Mapping[str, Any]) -> dict[str, Any]:
            body = f"To: {', '.join(arguments['to'])}\nSubject: {arguments['subject']}\n\n{arguments['body']}\n"
            entry = store.put(
                name=f"Reply draft {hashlib.sha256(body.encode()).hexdigest()[:8]}.txt", content=body.encode("utf-8"),
                media_type="text/plain", agent=self._agent(), requested_by="runtime:support-mailbox",
            )
            location = entry.get("location") or f"tier0:{self.tenant}/{entry['id']}"
            self.queue.add("mail-draft-ready", owner, f"A reply draft for a customer email is ready to check and send: {location}.",
                           reference=entry["id"])
            return {"draftId": entry["id"], "externalObjectRef": location}

        def escalate(item: Mapping[str, Any]) -> None:
            self.queue.add("mail-needs-person", owner,
                           f"A customer email needs a person (reason: {item['reason']}, thread {item['threadRef'][:12]}).",
                           reference=item["threadRef"][:40])

        events: list[tuple[str, str]] = []  # what this message caused, announced to triggers once it has been handled
        try:
            tickets = sidecar.tickets_store()
        except PermissionError:  # no built-in ticket store is bound: a thread for a person stays a notice only
            tickets = None

        def open_ticket(item: Mapping[str, Any]) -> Mapping[str, Any]:
            made = tickets.create(
                project="support", title=f"Customer email needs a person ({item['reason']})",
                description=f"Category {item['category']}; thread {item['threadRef'][:24]}. Open the thread in the mailbox; this ticket holds no message text.",
                requested_by="runtime:support-mailbox", agent=self._agent(),
            )
            if not made["duplicate"]:
                self.audit("ticket.opened", "runtime:support-mailbox",
                           {"ticketId": made["ticketId"], "project": "support", "reason": item["reason"], "threadRef": item["threadRef"][:24]})
                events.append(("ticket.created", made["ticketId"]))
            return made

        crm = self._crm_filing()
        filing = {"ledger": crm[0], "payloads": crm[1], "crm": crm[2], "contact_for": self._contact_for()} if crm is not None else {}
        mailbox = support.SupportMailbox(
            adapter, text.PrincipalDirectory(), {"mail.draft": draft}, classify=classify, answer=answer, escalate=escalate, ticket=open_ticket if tickets is not None else None,
            handled=support.MessageLog(self.data / "mail-handled"), audit=self.audit, conversations=self._conversation_recorder(), **filing,
        )
        def handle(raw: bytes, now: int) -> dict[str, Any]:
            del events[:]
            result = mailbox.handle(raw, now=now)
            if result.get("status") != "ignored":  # loops, spam and redeliveries are not events
                events.append(("mail.received", f"{result.get('threadRef')}:{result.get('messageRef')}"))
            for kind, reference in list(events):
                self.fire_event(kind, reference, now)
            return result

        return handle

    def _spool(self):
        settings = self.config["mailbox"]
        return _module("spool").MailSpool(self.data / "mail", tenant=self.tenant, addresses=settings["addresses"], audit=self.audit)

    def run_mailbox(self, now: int) -> dict[str, Any]:
        if self.config["mailbox"] is None:
            return {"skipped": "no-mailbox-configured"}
        if self.coworker is None:
            return {"skipped": "no-coworker-configured"}
        handle = self.mailbox_handler()
        return self._spool().drain(lambda raw: handle(raw, now), now=now)

    def deliver_notices(self, now: int) -> dict[str, Any]:
        delivery, capabilities = _module("delivery"), _module("capabilities")
        directory = delivery.Directory(self.config["people"])
        escalate_to = self.config["escalateTo"]
        sources = [delivery.handoff_source(self.data / "handoffs", escalate_to=escalate_to, now=now), self.queue.source()]
        grants: list[dict[str, Any]] = []
        grant_root = self.data / "provider-grants"
        if (grant_root / f"{self.tenant}.grants.json").is_file():
            grants = _module("grants").GrantRegistry(grant_root, tenant=self.tenant).list()
        store = _module("notices").NoticeStore(self.data / "notices", audit=self.audit)
        sources.append(delivery.connection_source(
            store, capabilities.read_connection_health(self.data), self.config["connectors"], grants, now=now, escalate_to=escalate_to,
        ))
        if self.config["export"] is not None:
            sources.append(self._export_alert_source(now))
        results = delivery.deliver(sources, directory, {"inbox": inbox_sender, **self.senders}, audit=self.audit)
        counts: dict[str, int] = {}
        for result in results:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        return counts

    def run_retention(self, now: int) -> dict[str, Any]:
        out: dict[str, Any] = {"artifacts": self._artifacts().purge_expired(now=now)}
        if self.config["mailbox"] is not None:
            out["mail"] = self._spool().purge(now=now)
        out["approvals"] = self.expire_approvals(now)
        return out

    def expire_approvals(self, now: int) -> int:
        """Remove approval requests and decisions older than ``MODEL_GARDEN_APPROVAL_TTL_HOURS`` and ledger each (ids only)."""
        approval = _module("approval")
        removed = approval.sweep(self.data / "approvals", now=now, ttl=approval.ttl_seconds())
        to = self.config["escalateTo"] or (self.config["mailbox"] or {}).get("owner")
        for item in removed:
            self.audit("approval.expired", IDENTITY, {"requestId": item["requestId"], "kind": item["kind"]})
            if item["kind"] == "request" and to:  # an action was waiting for a person and will now never run: say so once, by Tool name only
                self.queue.add("approval-expired", to, f"An approval request for {item.get('tool') or 'an action'} (ref {item['requestId'][:12]}) expired "
                               "with no decision, so it was not done. Ask for it again if it is still needed.", reference=item["requestId"][:40])
        return len(removed)

    def run_usage(self, now: int) -> dict[str, Any]:
        """Ledger the model usage each Hermes session store gained since the last pass.

        A store that does not exist yet (Hermes never started) is skipped. One that exists but cannot be read fails the job after
        the other stores are metered, so it is ledgered and shows in the heartbeat; nothing is lost, the next readable pass meters
        the whole difference.
        """
        if not self.usage_sources:
            return {"skipped": "no-usage-sources"}
        usage = _module("usage")
        meter = usage.UsageMeter(self.data / "worker" / "usage-cursor.sqlite")
        report: dict[str, Any] = {}
        unreadable = []
        for surface, path in sorted(self.usage_sources.items()):
            if not path.is_file():
                report[surface] = {"skipped": "no-store-yet"}
                continue
            try:
                facts = meter.collect(surface, usage.read_sessions(path))
            except usage.UsageError:
                unreadable.append(surface)
                continue
            recorded = [fact for fact in facts if "usageId" in fact]
            for fact in recorded:
                self.audit("usage.recorded", IDENTITY, usage.ledger_details(fact, agent=self._agent()))
            meter.commit(facts)
            report[surface] = {"recorded": len(recorded)}
        if unreadable:
            raise usage.UsageError(f"usage store unreadable: {', '.join(unreadable)}")
        return report

    def run_export(self, now: int) -> dict[str, Any]:
        """Copy new ledger records and a head manifest to the retention-locked bucket, as the separate export identity.

        The runtime cannot read the bucket back (only the verifier identity can), so the alert it can raise is about its own side:
        a local ledger that no longer matches what was exported (integrity), or one that is falling behind because exporting fails
        (lag, staleness). Each outcome is saved for ``deliver_notices``, which tells the escalation contact through the export monitor.
        """
        settings = self.config["export"]
        if settings is None:
            return {"skipped": "no-export-configured"}
        if not self.audit_path.is_file():
            return {"skipped": "no-ledger-yet"}
        ledger = _module("ledger")
        health = self._export_health()
        try:
            sink = self.export_sink
            if sink is None:
                token = ledger.metadata_token()
                if settings.get("impersonate"):
                    token = ledger.impersonated_token(settings["impersonate"], token, scope="https://www.googleapis.com/auth/devstorage.read_write")
                sink = ledger.GcsSink(settings["gcsBucket"], token, prefix=settings.get("prefix") or "")
            result = ledger.export_ledger(self.audit_path, sink, self._export_state_path())
        except ledger.LedgerTruncated:
            self._save_export_health({**health, "report": {"problems": ["local ledger no longer matches the export"], "lagEvents": 0}})
            raise
        except Exception:
            lag = self._export_lag()
            problems = []
            if lag > EXPORT_MAX_LAG_EVENTS:
                problems.append(f"export lag: {lag} event(s) waiting")
            last = health.get("lastSuccessAt")
            if lag and (last is None or now - last > EXPORT_MAX_STALE_SECONDS):
                problems.append("export stale: events are waiting and the export is failing")
            self._save_export_health({**health, "report": {"problems": problems, "lagEvents": lag}})
            raise
        self._save_export_health({"lastSuccessAt": now, "report": {"problems": [], "lagEvents": 0}})
        return result

    def _export_state_path(self) -> pathlib.Path:
        return self.data / "worker" / "export-state.json"

    def _export_health(self) -> dict[str, Any]:
        try:
            health = json.loads((self.data / "worker" / "export-health.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return health if isinstance(health, dict) else {}

    def _save_export_health(self, health: Mapping[str, Any]) -> None:
        path = self.data / "worker" / "export-health.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(health, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _export_lag(self) -> int:
        ledger = _module("ledger")
        try:
            events = ledger.read_chained_events(self.audit_path)
            cursor = json.loads(self._export_state_path().read_text(encoding="utf-8"))["sequence"] if self._export_state_path().is_file() else 0
        except (OSError, ValueError, KeyError, TypeError, ledger.LedgerExportError):
            return 0
        return max(0, (events[-1]["sequence"] if events else 0) - int(cursor))

    def _export_alert_source(self, now: int):
        """Due export alerts (from the last export outcome) as a notice source; delivery is recorded in the monitor's state."""
        delivery = _module("delivery")
        report = self._export_health().get("report")
        monitor = _module("export_monitor").ExportMonitor(self.data / "worker", audit=self.audit)
        escalate_to = self.config["escalateTo"]
        notices = monitor.evaluate(report, now=now, owner=escalate_to, escalate_to=escalate_to) if isinstance(report, Mapping) else []

        def mark(item: Mapping[str, Any], via: str) -> None:
            monitor.mark_notified(item["notice"], via=via, now=now)

        return delivery.source("export", notices, mark, reference=lambda notice: f"{str(notice['key'])[:12]}:{notice['kind']}")

    # -- loop ------------------------------------------------------------------------------------------

    def jobs(self) -> list[tuple[str, Callable[[int], dict[str, Any]]]]:
        return [
            ("scheduler", self.run_scheduler), ("mailbox", self.run_mailbox), ("crm-sync", self.run_crm_sync), ("calls", self.run_calls), ("changes", self.run_changes), ("events", self.run_events), ("knowledge", self.run_knowledge),
            ("notices", self.deliver_notices), ("retention", self.run_retention), ("usage", self.run_usage),
            ("export", self.run_export),
        ]

    def tick(self) -> dict[str, Any]:
        """One pass: every job whose interval has elapsed runs once; a failure is ledgered and the rest still run."""
        now = int(self.clock())
        report: dict[str, Any] = {}
        if not self._schedules_applied:
            report["schedules"] = self._guard("schedules", self.apply_schedules, now)
            self._schedules_applied = True
        for name, job in self.jobs():
            if now - self.last_run.get(name, float("-inf")) < self.config["intervals"][name]:
                continue
            self.last_run[name] = now
            report[name] = self._guard(name, job, now)
        self._beat(now, report)
        return report

    def _guard(self, name: str, job: Callable[[int], dict[str, Any]], now: int) -> dict[str, Any]:
        try:
            return job(now)
        except Exception as exc:  # noqa: BLE001 - one job never stops another; keep the type only
            try:
                self.audit("worker.job-failed", IDENTITY, {"job": name, "error": type(exc).__name__})
            except Exception:  # noqa: BLE001
                pass
            return {"failed": type(exc).__name__}

    def _beat(self, now: int, report: Mapping[str, Any]) -> None:
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.heartbeat.parent, 0o700)
        failed = sorted(name for name, value in report.items() if isinstance(value, Mapping) and "failed" in value)
        for name in report:  # do not reset a long-interval job on ticks when it did not run
            self._failure_streaks[name] = self._failure_streaks.get(name, 0) + 1 if name in failed else 0
        persistent = sorted(name for name, count in self._failure_streaks.items() if count >= 3)
        # Heartbeat carries ONLY fixed job names and counts, never provider errors or user data.
        document = {"at": now, "failed": failed, "persistentFailures": persistent}
        temporary = self.heartbeat.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.heartbeat)

    def run_forever(self, stop: threading.Event, *, pause: int = MIN_INTERVAL) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(pause)


def operational_health(heartbeat: pathlib.Path, *, now: float, max_age: int = 300) -> dict[str, Any]:
    """Small, payload-free worker readiness signal, independent of Docker liveness.

    Three failed attempts at the same job mean degradation; a healthy heartbeat is
    still live so restarting the worker cannot obscure a provider outage.
    """
    unavailable = {"status": "unavailable", "reason": "missing-or-invalid", "failedJobs": [], "persistentFailures": []}
    try:
        if heartbeat.is_symlink() or heartbeat.stat().st_size > 4096:
            return unavailable
        document = json.loads(heartbeat.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return unavailable
    if not isinstance(document, dict):
        return unavailable
    at = document.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return {**unavailable, "reason": "invalid"}
    failed = document.get("failed", [])
    persistent = document.get("persistentFailures", [])
    name_ok = lambda v: isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", v) is not None
    if not isinstance(failed, list) or not isinstance(persistent, list) or not all(name_ok(x) for x in failed + persistent):
        return {**unavailable, "reason": "invalid"}
    age = now - at
    if age < -30:
        return {**unavailable, "reason": "future"}
    if age > max_age:
        return {**unavailable, "reason": "stale"}
    return {"status": "degraded" if persistent else "healthy", "reason": "persistent-job-failures" if persistent else "recent",
            "failedJobs": sorted(set(failed)), "persistentFailures": sorted(set(persistent))}


def healthy(heartbeat: pathlib.Path, *, now: float, max_age: int = 300) -> bool:
    # A failing integration must be alerted to, not hidden behind Docker restarts.
    return operational_health(heartbeat, now=now, max_age=max_age)["status"] != "unavailable"


def build(args: argparse.Namespace, environ: Mapping[str, str]) -> Worker:
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config and args.config.is_file() else {}
    if not config.get("deployedBy") and environ.get("MODEL_GARDEN_DEPLOYED_BY", "").strip():
        config["deployedBy"] = environ["MODEL_GARDEN_DEPLOYED_BY"].strip()
    coworker = None
    settings = config.get("coworker")
    if settings:
        key = environ.get("MODEL_GARDEN_COWORKER_API_KEY", "")
        coworker = _module("coworker").CoworkerClient(settings["url"], key, model=settings.get("model", "hermes-agent"))
    senders: dict[str, Any] = {}
    if environ.get("MODEL_GARDEN_SLACK_BOT_TOKEN"):  # notices to a person's Slack direct-message channel (see the channel gateway)
        senders["slack"] = _module("transports").SlackTransport(environ["MODEL_GARDEN_SLACK_BOT_TOKEN"]).send_text
    usage_sources = _module("usage").parse_sources(environ.get("MODEL_GARDEN_USAGE_SOURCES", ""))
    voice = environ.get("MODEL_GARDEN_VOICE_TRANSCRIPTS", "").strip()
    return Worker(args.desired_state, args.data_root, config, coworker=coworker, senders=senders, usage_sources=usage_sources,
                  voice_transcripts=pathlib.Path(voice) if voice else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--desired-state", type=pathlib.Path,
                        default=pathlib.Path(os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--data-root", type=pathlib.Path, default=pathlib.Path(os.environ.get("MODEL_GARDEN_DATA_ROOT", "/opt/data/.modelgarden")))
    parser.add_argument("--config", type=pathlib.Path,
                        default=pathlib.Path(os.environ.get("MODEL_GARDEN_WORKER_CONFIG", "/opt/profile-seed/.modelgarden/worker.json")))
    parser.add_argument("--once", action="store_true", help="run one pass and print what each job did")
    parser.add_argument("--health", action="store_true", help="exit 0 when the last pass is recent (Docker liveness only)")
    parser.add_argument("--readiness", action="store_true", help="emit payload-free operational status; nonzero on persistent job failures or stalled worker")
    args = parser.parse_args(argv)
    if args.readiness:
        status = operational_health(args.data_root / "worker" / "heartbeat.json", now=time.time())
        print(json.dumps(status, sort_keys=True))
        return 0 if status["status"] == "healthy" else 1
    if args.health:
        return 0 if healthy(args.data_root / "worker" / "heartbeat.json", now=time.time()) else 1
    try:
        worker = build(args, os.environ)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:  # RuntimeError: CoworkerError, TransportError, LedgerExportError
        print(f"Runtime worker failed to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.once:
        print(json.dumps(worker.tick(), indent=2, sort_keys=True, default=str))
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker.run_forever(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
