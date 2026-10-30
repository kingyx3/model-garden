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
import functools
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import secrets
import signal
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_INTERVALS = {"scheduler": 60, "mailbox": 30, "notices": 60, "retention": 3600, "usage": 300, "export": 300, "crm-sync": 120, "calls": 120, "changes": 60, "events": 30, "knowledge": 21_600, "task-reports": 60, "spend": 300, "digest": 3600}
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
    "unanswered": "platform/core/unanswered.py",
    "inbox": "platform/core/inbox.py",
    "task_inbox": "platform/core/task_inbox.py",
    "heartbeat": "platform/core/heartbeat.py",
    "pause": "platform/core/pause.py",
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
    allowed = {"deployedBy", "intervals", "people", "escalateTo", "owners", "mailbox", "coworker", "connectors", "artifactRetentionDays", "export", "spend", "digest"}
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
    spend = document.get("spend")
    if spend is not None:
        if not isinstance(spend, Mapping) or set(spend) - {"capCents", "warnAtPercent"}:
            raise WorkerConfigError("spend holds capCents and optional warnAtPercent")
        cap = spend.get("capCents")
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 100_000_000:
            raise WorkerConfigError("spend.capCents must be a whole number of US cents, 1 or more")
        warn = spend.get("warnAtPercent", [80])
        if not isinstance(warn, list) or len(warn) > 5 or not all(isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 99 for item in warn):
            raise WorkerConfigError("spend.warnAtPercent is up to five whole percentages from 1 to 99")
        spend = {"capCents": cap, "warnAtPercent": sorted(set(warn))}
    digest = document.get("digest")
    if digest is not None:
        if not isinstance(digest, Mapping) or set(digest) - {"to", "weekday", "hour", "utcOffsetMinutes"}:
            raise WorkerConfigError("digest holds to, weekday, hour and utcOffsetMinutes")
        _person(digest.get("to"), "digest.to")
        weekday, hour, offset = digest.get("weekday", 0), digest.get("hour", 8), digest.get("utcOffsetMinutes", 0)
        for name, value, low, high in (("weekday", weekday, 0, 6), ("hour", hour, 0, 23), ("utcOffsetMinutes", offset, -720, 840)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise WorkerConfigError(f"digest.{name} must be a whole number from {low} to {high}" + (" (Monday is 0)" if name == "weekday" else ""))
        digest = {"to": digest["to"], "weekday": weekday, "hour": hour, "utcOffsetMinutes": offset}
    escalate_to = document.get("escalateTo")
    if escalate_to is not None:
        _person(escalate_to, "escalateTo")
    people = document.get("people") or {}
    _module("delivery").Directory(people)  # validates: no credentials, one channel each
    retention = document.get("artifactRetentionDays")
    if retention is not None and (type(retention) is not int or not 1 <= retention <= 3650):
        raise WorkerConfigError("artifactRetentionDays must be 1 to 3650 days")
    connectors = document.get("connectors") or {}
    if not isinstance(connectors, Mapping):
        raise WorkerConfigError("connectors must be the binding's connector map")
    return {
        "deployedBy": document.get("deployedBy") or None, "intervals": intervals, "people": dict(people), "escalateTo": escalate_to,
        "owners": owners, "mailbox": dict(mailbox) if mailbox else None, "coworker": dict(coworker) if coworker else None,
        "connectors": dict(connectors), "artifactRetentionDays": document.get("artifactRetentionDays"),
        "export": dict(export) if export else None, "spend": spend, "digest": digest,
    }


class NoticeQueue:
    """Notices the worker itself produces (schedule outcomes, mailbox threads), delivered like hand-off notices."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / "worker-notices.json"

    MAX_BYTES = 2_000_000

    @staticmethod
    def _private_file(info: os.stat_result) -> None:
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("unsafe worker notice state; operator repair required")

    def _private_root(self) -> os.stat_result:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise ValueError("unsafe worker notice directory; operator repair required")
        return info

    def _read_state(self) -> dict[str, Any]:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"pending": {}}
        with os.fdopen(descriptor, "rb") as stream:
            self._private_file(os.fstat(stream.fileno()))
            raw = stream.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError("worker notice state exceeds its size limit")
        try:
            state = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ValueError("invalid worker notice state; operator repair required") from None
        if (not isinstance(state, dict) or set(state) != {"pending"}
                or not isinstance(state["pending"], dict)
                or len(state["pending"]) > MAX_QUEUE):
            raise ValueError("invalid worker notice state; operator repair required")
        for key, notice in state["pending"].items():
            if (not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{24}", key) is None
                    or not isinstance(notice, dict)
                    or not {"key", "kind", "to", "text", "reference"} <= set(notice)
                    or set(notice) - {"key", "kind", "to", "text", "reference", "inboxPosted"}
                    or notice["key"] != key
                    or not isinstance(notice["kind"], str) or len(notice["kind"]) > 128
                    or not isinstance(notice["text"], str) or len(notice["text"]) > 600
                    or not isinstance(notice["reference"], str) or len(notice["reference"]) > 40
                    or (notice["to"] is not None and
                        (not isinstance(notice["to"], str) or len(notice["to"]) > 256))
                    or ("inboxPosted" in notice and type(notice["inboxPosted"]) is not bool)):
                raise ValueError("invalid worker notice entries; operator repair required")
        return state

    @contextlib.contextmanager
    def _locked(self):
        directory = self._private_root()
        lock = self.root / ".worker-notices.lock"
        descriptor = os.open(
            lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            self._private_file(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state = self._read_state()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                # The worker must never publish an alert to a replaced directory.
                current = self.root.lstat()
                if (current.st_dev, current.st_ino) != (directory.st_dev, directory.st_ino):
                    raise ValueError("worker notice directory changed during update")
                self._private_root()
                temporary = self.root / f".worker-notices-{secrets.token_hex(16)}.tmp"
                created = False
                try:
                    handle = os.open(
                        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0), 0o600
                    )
                    created = True
                    with os.fdopen(handle, "w", encoding="utf-8") as stream:
                        stream.write(json.dumps(state, sort_keys=True) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Refuse a replaced or newly linked state path, not just a
                    # malicious path that was present at initial read time.
                    try:
                        old = self.path.lstat()
                    except FileNotFoundError:
                        old = None
                    if old is not None:
                        self._private_file(old)
                    os.replace(temporary, self.path)
                    directory_fd = os.open(
                        self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    try:
                        opened = os.fstat(directory_fd)
                        if (opened.st_dev, opened.st_ino) != (directory.st_dev, directory.st_ino):
                            raise ValueError("worker notice directory changed during update")
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    if created:
                        temporary.unlink(missing_ok=True)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def add(self, kind: str, to: str | None, text: str, *, reference: str) -> str:
        key = hashlib.sha256(f"{kind}|{to}|{reference}".encode()).hexdigest()[:24]
        with self._locked() as state:
            if key not in state["pending"]:
                if len(state["pending"]) >= MAX_QUEUE:
                    # Never report a successful human handoff when the durable
                    # queue silently discarded the notice.
                    raise RuntimeError("worker notice queue full; retry the human handoff")
                state["pending"][key] = {"key": key, "kind": kind, "to": to, "text": text[:600], "reference": reference[:40]}
        return key

    def source(self, *, kind_filter: Callable[[str], bool] | None = None):
        with self._locked() as state:
            # A local inbox is not an external send. Keep posted notices until
            # an explicit host-operator acknowledgement; do not post them again
            # on every clock tick or silently remove them on a no-op sender.
            notices = [dict(notice) for notice in state["pending"].values()
                       if not notice.get("inboxPosted") and
                       (kind_filter is None or kind_filter(str(notice.get("kind"))))]

        def mark(item: Mapping[str, Any], via: str) -> None:
            with self._locked() as state:
                key = item["notice"]["key"]
                if key in state["pending"]:
                    if via == "inbox":
                        state["pending"][key]["inboxPosted"] = True
                    else:
                        state["pending"].pop(key)

        return _module("delivery").source("worker", notices, mark, reference=lambda notice: f"{notice['reference']}:{notice['kind']}")

    def ack_inbox(self, key: str, *, actor: str, audit: Callable[[str, str, dict[str, Any]], Any]) -> None:
        """Acknowledge a posted local notice from a host operator, never as client-owner consent.

        Ledger first, then remove: a failed ledger leaves the notice visible.
        A crash after the receipt can repeat an acknowledgement, not lose it.
        """
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{24}", key) is None:
            raise ValueError("no such inbox notice")
        if not isinstance(actor, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", actor) is None:
            raise ValueError("host operator identity is invalid")
        with self._locked() as state:
            notice = state["pending"].get(key)
            if not isinstance(notice, dict) or notice.get("inboxPosted") is not True:
                raise ValueError("no such inbox notice")
            audit("notice.operator-ack", f"host-operator:{actor}", {
                "noticeKey": key, "kind": notice["kind"], "reference": notice["reference"],
            })
            del state["pending"][key]

    def pending(self) -> list[dict[str, Any]]:
        with self._locked() as state:
            return list(state["pending"].values())


def inbox_sender(address: str, text: str) -> None:
    """The built-in inbox is a channel only when the person's entry says so; nothing is sent, the notice is listed there."""
    return None


def _pause_fenced(job: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Fence the *entire* worker-owned material job, including model calls and local writes.

    Checking pause before a long-running operation is insufficient: an owner
    could otherwise receive a confirmed pause while a scheduled run, mailbox
    draft, event trigger or CRM retry was still executing. A shared lease
    serializes those operations with the owner's exclusive transition; the
    status is checked *inside* the lease. Nested event calls use shared leases.
    This is a cooperative host-local fence, not remote provider cancellation.
    """
    @functools.wraps(job)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        with _module("pause").PauseStore(self.data).effect_guard():
            if self._paused():
                return {"skipped": "paused"}
            return job(self, *args, **kwargs)

    return guarded


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
        # The voice overlay pins the exact LiveKit worker identity. A missing
        # overlay setting falls back to the compiled Agent only for standalone
        # worker/fixture use; records without attribution are never trusted.
        self.voice_agent = os.environ.get("MODEL_GARDEN_VOICE_AGENT_NAME", self._agent())
        if (not isinstance(self.voice_agent, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.voice_agent) is None):
            raise WorkerConfigError("MODEL_GARDEN_VOICE_AGENT_NAME must be a valid voice agent identity")
        self.queue = NoticeQueue(self.data / "worker")
        self.last_run: dict[str, float] = {}
        self.heartbeat = self.data / "worker" / "heartbeat.json"
        self._failure_streaks: dict[str, int] = self._load_failure_streaks()  # persisted across worker restarts
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

    @_pause_fenced
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

    @_pause_fenced
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

    def _paused(self) -> bool:
        return _module("pause").PauseStore(self.data).is_paused()

    @_pause_fenced
    def run_scheduler(self, now: int) -> dict[str, Any]:
        if self._paused():
            return {"skipped": "paused"}
        if self.coworker is None:
            return {"skipped": "no-coworker-configured"}
        scheduler = self._scheduler()
        due = scheduler.run_due(now=now)
        resumed = scheduler.resume_waiting(now=now)
        return {"runs": len(due["runs"]), "resumed": len(resumed["runs"])}

    @_pause_fenced
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

    @_pause_fenced
    def run_events(self, now: int) -> dict[str, Any]:
        """Fire ``chat.message`` for what the gateway reported and ``ticket.created`` for tickets nobody announced; once per event."""
        if self._paused():
            return {"skipped": "paused"}  # events stay in the outbox and fire after resume
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
        """File completed voice calls only while the shared emergency-pause boundary admits work."""
        path = self.voice_transcripts
        if path is None or not path.is_file():
            return {"skipped": "no-voice-transcripts"}
        # Transcript filing includes model summarization, local timeline writes and
        # failure receipts, not just the sidecar's governed CRM Tool. The worker
        # must claim the same cooperative host-local lease as material Tools so
        # an acknowledged owner pause cannot race these unguarded operations.
        with _module("pause").PauseStore(self.data).effect_guard():
            if self._paused():
                return {"skipped": "paused"}  # leave transcripts untouched for resume
            sidecar = self._sidecar(CALL_FILING_IDENTITY)
            try:
                store = sidecar.crm_store()
            except PermissionError:
                return {"skipped": "no-crm-bound"}
            filing = _module("filing")
            # Validate one atomic generation before any model, CRM, timeline or
            # ledger effect. A corrupt late failure/call must not be hidden by
            # a valid prefix or by a concurrent voice-writer replacement.
            snapshot = filing.read_voice_records(path)
            # Check the complete snapshot before any model request, CRM write,
            # conversation timeline or failure receipt. Never attribute another
            # voice Agent's transcript to this coworker's tenant or ledger.
            for row in snapshot:
                if row.get("type") in {"CallTranscript", "TranscriptWriteFailed"} and row.get("agent") != self.voice_agent:
                    raise ValueError("voice record agent mismatch; operator reconciliation required")
            records = filing.call_transcripts(snapshot)
            write_failures = filing.transcript_failures(snapshot)
            try:
                records = _module("call_requests").attach_request_ids(records, _module("audit_query").read_ledger(self.audit_path))
            except ValueError:  # an unreadable ledger only means the calls are filed without request ids
                pass
            summarizer = _module("call_summary").CallSummarizer(
                self.coworker, cache_path=self.data / "call-summaries.json",
                completion_path=self.data / "summary-completions" / "call-summary-completions.jsonl")
            totals = filing.file_call_transcripts(sidecar, store, records, recorder=self._conversation_recorder(), summarizer=summarizer)
            ledgered = filing.ledger_transcript_failures(_module("govern"), self.audit_path, write_failures, performed_by=CALL_FILING_IDENTITY)
            totals["notFiled"] = totals.pop("failed")  # a "failed" key would read as the job itself failing
            return {"records": len(records), **totals, "writeFailures": ledgered, "summaries": summarizer.counts}

    def _queue_once(self, kind: str, reference: str, to: str, text: str, *, notice_reference: str | None = None) -> bool:
        """Persist a once-only owner notice *before* remembering that it was queued.

        The marker and the notice use separate files: an interrupted marker
        write can cause an at-least-once duplicate, but a full queue, failed
        write or crash must never permanently suppress an undelivered alert.
        A host-local lock serializes competing worker instances.
        """
        path = self.data / "worker" / "told.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path.parent / ".told.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            digest = hashlib.sha256(f"{kind}|{reference}".encode("utf-8")).hexdigest()[:24]
            try:
                known = [item for item in json.loads(path.read_text(encoding="utf-8")) if isinstance(item, str)]
            except FileNotFoundError:
                known = []
            if digest in known:
                return False
            # This can raise (queue full or unavailable). In that case the
            # marker is untouched, and the next worker pass can try again.
            self.queue.add(kind, to, text, reference=(notice_reference or reference)[:40])
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(prefix=".told-", dir=path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps([*known, digest][-MAX_QUEUE:]) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    pathlib.Path(temporary).unlink(missing_ok=True)
            return True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def month_to_date_micros(self, window: str) -> int:
        """Model cost metered into the ledger so far in the UTC calendar month ``window`` (``YYYY-MM``), in micro-dollars (an estimate)."""
        total = 0
        try:
            lines = self.audit_path.open(encoding="utf-8")
        except FileNotFoundError:
            return 0
        with lines:
            for line in lines:
                if '"usage.recorded"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                cost = (event.get("details") or {}).get("costMicrosUsd")
                if (event.get("action") == "usage.recorded" and str(event.get("timestamp", ""))[:7] == window
                        and isinstance(cost, int) and not isinstance(cost, bool) and cost > 0):
                    total += cost
        return total

    def _hold_unmetered_spend(self, now: int) -> dict[str, Any]:
        """Fail closed for autonomous worker work when a configured cap cannot be reconciled.

        A spend-cap hold also blocks ordinary owner resume. Only a subsequent
        successful metering pass and cap reconciliation may release it. This is
        a worker-local safety gate, not a reservation of provider-side spend.
        """
        window = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m")
        store = _module("pause").PauseStore(self.data)
        state = store.status()
        if not state["paused"]:
            store.pause(IDENTITY, "monthly model spend metering unavailable", now, cause="spend-cap", window=window)
            self.audit("spend.metering-unavailable", IDENTITY, {"window": window})
        to = self.config["escalateTo"] or (self.config["mailbox"] or {}).get("owner")
        if to:
            self._queue_once(
                "spend-metering-unavailable", f"{window}|metering", to,
                "Model-spend metering is unavailable, so autonomous coworker work is held until the worker "
                "can read every configured Hermes usage store and reconcile the monthly cap. "
                "This is a safety hold, not proof that the cap was reached.",
                notice_reference=f"{window}:metering",
            )
        return {"held": True, "reason": "metering-unavailable"}

    def run_spend(self, now: int) -> dict[str, Any]:
        """Warn at configured percentages of a monthly cap, and at the cap pause the coworker (#238, using the #232 pause).

        The month is the UTC calendar month and the figure is what Hermes estimated for each session, so it is a guard rail, not an
        invoice. A pause the cap caused is re-applied while spending is still at or over the cap (resuming by hand does not outrun it;
        raise the cap or wait for the next month) and clears itself when a new month starts under the cap; a pause an owner made is never
        cleared here. Notices go to ``escalateTo`` (else the mailbox owner) once per threshold per month.
        """
        settings = self.config["spend"]
        if settings is None:
            return {"skipped": "no-spend-cap-configured"}
        window = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m")
        cap_micros = settings["capCents"] * 10_000
        spent = self.month_to_date_micros(window)
        percent = spent * 100 // cap_micros
        to = self.config["escalateTo"] or (self.config["mailbox"] or {}).get("owner")
        store = _module("pause").PauseStore(self.data)
        state = store.status()
        if state["paused"] and state.get("cause") == "spend-cap" and spent < cap_micros:
            # The worker alone reconciles a cap pause after confirming usage is
            # below the *current* limit (a new month or an approved cap increase).
            # An owner resume/repause cannot erase this hold between worker passes.
            cap_window = state.get("window")
            if isinstance(cap_window, str) and store.clear_spend_cap(now, expected_window=cap_window):
                metering_hold = state.get("reason") == "monthly model spend metering unavailable"
                self.audit("spend.metering-restored" if metering_hold else "spend.cap-cleared", IDENTITY,
                           {"window": window, "previousWindow": cap_window})
                if metering_hold and to:
                    self._queue_once(
                        "spend-metering-restored", f"{window}|metering-restored", to,
                        "Model-spend metering has recovered and the current monthly estimate is below the configured cap. "
                        "The worker's metering safety hold was released; this is not an invoice reconciliation.",
                        notice_reference=f"{window}:restored",
                    )
                state = {"paused": False}
        told = 0
        for threshold in settings["warnAtPercent"]:
            if percent >= threshold and spent < cap_micros and to:
                if self._queue_once(
                    "spend-warning", f"{window}|{threshold}|{settings['capCents']}", to,
                    f"Model spend this month is about {int(percent)}% of the {settings['capCents'] / 100:.2f} USD cap "
                    "(an estimate). At the cap the coworker stops new actions.",
                    notice_reference=f"{window}:{threshold}",
                ):
                    told += 1
                    self.audit("spend.warning", IDENTITY, {"window": window, "threshold": threshold, "percent": int(percent)})
        paused_now = False
        if spent >= cap_micros:
            if not state["paused"]:
                store.pause(IDENTITY, f"monthly spend cap reached ({settings['capCents'] / 100:.2f} USD)", now, cause="spend-cap", window=window)
                paused_now = True
                self.audit("spend.cap-reached", IDENTITY, {"window": window, "capCents": settings["capCents"], "spentMicrosUsd": spent})
            # The cap hold persists after this pass. A queue outage during the
            # transition must not make the notification impossible to retry.
            if to:
                self._queue_once(
                    "spend-cap", f"{window}|{settings['capCents']}", to,
                    f"The monthly model-spend cap of {settings['capCents'] / 100:.2f} USD (an estimate) was reached, so the coworker "
                    "has stopped new actions. Raise the cap or wait for next month; then it resumes.",
                    notice_reference=f"{window}:cap",
                )
        return {"window": window, "spentMicrosUsd": spent, "percent": int(percent), "warned": told, "paused": paused_now}

    DIGEST_GRACE = 2 * 86_400  # a digest later than this after its time is skipped rather than back-filled

    @_pause_fenced
    def run_digest(self, now: int) -> dict[str, Any]:
        """Send the owner one short weekly summary of counts from the ledger and the inbox (#236); once per window, counts only.

        The window is the seven days before the most recent configured weekday and hour in the configured fixed UTC offset (no daylight
        saving). A window that was missed by more than two days is skipped, not back-filled. The text holds no message, customer or
        file; conversations are approximated by metered model sessions.
        """
        settings = self.config["digest"]
        if settings is None:
            return {"skipped": "no-digest-configured"}
        offset = settings["utcOffsetMinutes"] * 60
        local = dt.datetime.fromtimestamp(now + offset, dt.timezone.utc)
        day = local.replace(hour=settings["hour"], minute=0, second=0, microsecond=0) - dt.timedelta(days=(local.weekday() - settings["weekday"]) % 7)
        if day > local:
            day -= dt.timedelta(days=7)
        end = int(day.timestamp()) - offset
        if now - end > self.DIGEST_GRACE:
            return {"skipped": "window-missed"}
        marker = self.data / "worker" / "digest-sent.json"
        try:
            last = int(json.loads(marker.read_text(encoding="utf-8")).get("last", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            last = 0
        if end <= last:
            return {"skipped": "already-sent"}
        counts = self._ledger_counts(end - 7 * 86_400, end)
        inbox = _module("inbox").collect(
            approvals=self.data / "approvals", handoffs=self.data / "handoffs", contacts=self.data / "contacts", governance=self.data,
            tickets=self.data / "tickets", crm_sync=self.data / "crm-sync", unanswered=self.data / "unanswered", now=now)["counts"]
        channels = ", ".join(f"{name} {count}" for name, count in sorted(counts["channels"].items(), key=lambda item: (-item[1], item[0]))[:3]) or "none"
        text = (f"Weekly summary, 7 days to {day.date().isoformat()}: {counts['executed']} action(s) done, {counts['failed']} failed; approvals "
                f"{counts['approvalsAsked']} asked, {counts['approved']} approved, {counts['rejected']} rejected; {counts['denied']} denied, "
                f"{counts['handoffs']} hand-off(s). Model use: {counts['sessions']} session(s) ({channels}), about "
                f"{counts['costMicros'] / 1_000_000:.2f} USD (estimate). Waiting now: {inbox['approvals']} approval(s), {inbox['handoffs']} hand-off(s) "
                f"({inbox['overdueHandoffs']} overdue), {inbox['tickets']} ticket(s), {inbox['crmFilings']} CRM filing(s) blocked, "
                f"{inbox['unanswered']} unanswered question group(s)."
                + (" The coworker is paused." if inbox.get("paused") else ""))
        self.queue.add("digest", settings["to"], text, reference=f"digest:{end}"[:40])
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps({"last": end}) + "\n", encoding="utf-8")
        os.replace(temporary, marker)
        self.audit("digest.sent", IDENTITY, {"windowEnd": end, "recipient": settings["to"], "counts": {k: v for k, v in counts.items() if k != "channels"}})
        return {"windowEnd": end, "sent": True}

    def _ledger_counts(self, start: int, end: int) -> dict[str, Any]:
        """Counts of what the ledger recorded in ``[start, end)``: outcomes only, never arguments or text."""
        counts: dict[str, Any] = {"executed": 0, "failed": 0, "approvalsAsked": 0, "approved": 0, "rejected": 0, "denied": 0, "handoffs": 0,
                                  "sessions": 0, "costMicros": 0, "channels": {}}
        try:
            lines = self.audit_path.open(encoding="utf-8")
        except FileNotFoundError:
            return counts
        with lines:
            for line in lines:
                try:
                    event = json.loads(line)
                    moment = dt.datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    continue
                if not start <= moment < end:
                    continue
                kind, outcome = event.get("event"), event.get("outcome")
                key = {("execution", "succeeded"): "executed", ("execution", "failed"): "failed", ("approval", "pending"): "approvalsAsked",
                       ("approval", "approved"): "approved", ("approval", "rejected"): "rejected", ("authorization", "deny"): "denied",
                       ("authorization", "hand-off"): "handoffs"}.get((kind, outcome))
                if key:
                    counts[key] += 1
                elif kind == "operator-action" and event.get("action") == "usage.recorded":
                    details = event.get("details") or {}
                    counts["sessions"] += 1
                    cost = details.get("costMicrosUsd")
                    counts["costMicros"] += cost if isinstance(cost, int) and not isinstance(cost, bool) and cost > 0 else 0
                    channel = re.sub(r"[^a-z0-9-]", "", str(details.get("channel") or "unknown").lower())[:20] or "unknown"
                    counts["channels"][channel] = counts["channels"].get(channel, 0) + 1
        return counts

    def run_task_reports(self, now: int) -> dict[str, Any]:
        """Tell each person whose task an Agent has reported (done, declined or failed), once, through their own channel (#229).

        Only the task reference, the Agent and the outcome go in the notice; the report text stays in the task inbox and never reaches the
        ledger or the notice queue. It keeps running while the coworker is paused, because a person is waiting to hear.
        """
        path = self.data / "inbox.json"
        if not path.is_file():
            return {"told": 0}
        told = 0
        for entry in _module("task_inbox").TaskInbox(path).reported():
            if self._queue_once(
                "task-report", f"{entry['taskId']}|{entry['state']}", entry["person"],
                f"Your task {entry['taskId']} for {entry['agent']} is {entry['state']}. Ask for the result in a direct chat or from the operator.",
                notice_reference=entry["taskId"],
            ):
                told += 1
        return {"told": told}

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
            if phase == "stale":
                text = (f"The business-rules source {name} passed its review date ({spec['reviewBy']}), so the coworker has stopped answering from it. "
                        "Confirm the content is still right, set a later reviewBy and redeploy.")
            else:
                text = f"The business-rules source {name} is due for review on {spec['reviewBy']} ({days} day(s) left). After that the coworker stops answering from it."
            if self._queue_once("knowledge-review", f"{name}|{spec['reviewBy']}|{phase}", to, text,
                                notice_reference=f"{name}:{phase}"):
                if phase == "stale":
                    stale += 1
                else:
                    due += 1
        return {"dueSoon": due, "stale": stale}

    def _blocked_notified(self) -> pathlib.Path:
        return self.data / "crm-sync" / "blocked-notified.json"

    @_pause_fenced
    def run_crm_sync(self, now: int) -> dict[str, Any]:
        if self._paused():
            return {"skipped": "paused"}  # filings stay queued; nothing is sent to the CRM while paused
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

    @_pause_fenced
    def run_mailbox(self, now: int) -> dict[str, Any]:
        if self._paused():
            return {"skipped": "paused"}  # mail stays in the spool, unread by the coworker, until resume
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
        # Safety/owner action notices must continue during pause; the optional
        # weekly digest is not a safety alert and must be separately fenced.
        sources = [delivery.handoff_source(self.data / "handoffs", escalate_to=escalate_to, now=now),
                   self.queue.source(kind_filter=lambda kind: kind != "digest")]
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
        senders = {"inbox": inbox_sender, **self.senders}
        results = delivery.deliver(sources, directory, senders, audit=self.audit)
        # An already queued digest must not escape the pause via the normal
        # safety-notice drain. Hold the lease through the actual send/mark so
        # owner pause cannot be acknowledged while a digest is in flight.
        if not self._paused():
            with _module("pause").PauseStore(self.data).effect_guard():
                if not self._paused():
                    results.extend(delivery.deliver(
                        [self.queue.source(kind_filter=lambda kind: kind == "digest")],
                        directory, senders, audit=self.audit,
                    ))
        counts: dict[str, int] = {}
        for result in results:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        return counts

    def run_retention(self, now: int) -> dict[str, Any]:
        out: dict[str, Any] = {"artifacts": self._artifacts().purge_expired(now=now)}
        if self.config["mailbox"] is not None:
            out["mail"] = self._spool().purge(now=now)
        out["approvals"] = self.expire_approvals(now)
        out["unanswered"] = self.purge_unanswered(now)
        return out

    def purge_unanswered(self, now: int) -> int:
        """Remove unanswered-question groups not seen for 90 days (#237); the text is a bounded sample and is not kept for ever."""
        directory = self.data / "unanswered"
        if not directory.is_dir():
            return 0
        store_module = _module("unanswered")
        removed = 0
        for path in sorted(directory.glob("*.unanswered.json")):
            store = store_module.UnansweredStore(directory, tenant=path.name[: -len(".unanswered.json")], audit=self.audit)
            removed += store.purge_expired(now)
        return removed

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
            # Even with no measurable lag (or an unreadable cursor), a failed
            # export attempt is not a healthy export. Never include provider
            # exception text or credentials in the durable health record.
            problems = ["export failure: export attempt did not complete"]
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
            ("scheduler", self.run_scheduler), ("mailbox", self.run_mailbox), ("crm-sync", self.run_crm_sync), ("calls", self.run_calls), ("changes", self.run_changes), ("events", self.run_events), ("knowledge", self.run_knowledge), ("task-reports", self.run_task_reports), ("spend", self.run_spend), ("digest", self.run_digest),
            ("notices", self.deliver_notices), ("retention", self.run_retention), ("usage", self.run_usage),
            ("export", self.run_export),
        ]

    def tick(self) -> dict[str, Any]:
        """One pass: every job whose interval has elapsed runs once; a failure is ledgered and the rest still run."""
        now = int(self.clock())
        report: dict[str, Any] = {}
        blocked = False
        if self.config["spend"] is not None:
            # When a cap is configured, every worker tick first collects the
            # latest session counters and reconciles the cap. The old ordering
            # ran schedules/mailbox before both jobs, then evaluated the cap
            # before the newly metered usage. Do not honor slower intervals for
            # these two safety checks while a cap is enabled.
            report["usage"] = self._guard("usage", self.run_usage, now)
            self.last_run["usage"] = now
            metered = bool(self.usage_sources) and all(
                isinstance(report["usage"].get(surface), Mapping)
                and type(report["usage"][surface].get("recorded")) is int
                and report["usage"][surface]["recorded"] >= 0
                for surface in self.usage_sources
            )
            if metered:
                report["spend"] = self._guard("spend", self.run_spend, now)
                if "failed" in report["spend"]:
                    report["spendHold"] = self._guard("spend-hold", self._hold_unmetered_spend, now)
                    blocked = True
            else:
                report["spend"] = self._guard("spend-hold", self._hold_unmetered_spend, now)
                blocked = True
            self.last_run["spend"] = now
        if not self._schedules_applied and not blocked:
            report["schedules"] = self._guard("schedules", self.apply_schedules, now)
            # A paused or failed startup must be retried after resume/recovery;
            # otherwise the one-shot flag silently drops the workspace schedules.
            self._schedules_applied = (
                report["schedules"].get("skipped") != "paused"
                and "failed" not in report["schedules"]
            )
        for name, job in self.jobs():
            if self.config["spend"] is not None and name in ("usage", "spend"):
                continue  # already run as the mandatory preflight
            if blocked and name not in ("notices", "retention", "export"):
                continue  # preserve human alerts and housekeeping, never start model work
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

    def _failure_state_path(self) -> pathlib.Path:
        return self.heartbeat.with_name("failure-streaks.json")

    def _failure_marker_path(self) -> pathlib.Path:
        return self.heartbeat.with_name("failure-streaks.initialized")

    def _load_failure_streaks(self) -> dict[str, int]:
        """Preserve consecutive job failures across process restarts; corrupt history is not a clean bill of health."""
        heartbeat = _module("heartbeat")
        history, marker = self._failure_state_path(), self._failure_marker_path()
        try:
            history.lstat()  # unlike exists(), detect dangling symlinks
        except FileNotFoundError:
            try:
                marker.lstat()
            except FileNotFoundError:
                # Pre-upgrade installations have a heartbeat but no durable streak history.
                # Carry forward any already-persistent failure; partial streaks cannot be recovered.
                try:
                    self.heartbeat.lstat()
                except FileNotFoundError:
                    return {}  # genuinely fresh volume
                except OSError:
                    raise WorkerConfigError("previous worker heartbeat is untrusted") from None
                try:
                    previous = heartbeat._read_pinned(self.heartbeat)
                    allowed = set(DEFAULT_INTERVALS) | {"schedules", "alerts"}
                    if (not isinstance(previous, dict) or not isinstance(previous.get("persistentFailures"), list) or
                            any(not isinstance(name, str) or name not in allowed
                                for name in previous["persistentFailures"])):
                        raise ValueError("invalid previous heartbeat")
                except (OSError, ValueError, TypeError, UnicodeError):
                    raise WorkerConfigError("previous worker heartbeat is untrusted") from None
                return {name: 3 for name in previous["persistentFailures"]}
            raise WorkerConfigError("worker failure history is missing after initialization") from None
        try:
            document = heartbeat._read_pinned(history)
            if not isinstance(document, dict) or set(document) != {"schemaVersion", "streaks"} or document["schemaVersion"] != 1:
                raise ValueError("invalid failure history")
            streaks = document["streaks"]
            allowed = set(DEFAULT_INTERVALS) | {"schedules", "alerts"}
            if (not isinstance(streaks, dict) or len(streaks) > len(allowed) or
                    any(name not in allowed or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 3
                        for name, count in streaks.items())):
                raise ValueError("invalid failure history")
            return dict(streaks)
        except (OSError, ValueError, TypeError, UnicodeError):
            raise WorkerConfigError("worker failure history is unreadable or untrusted") from None

    def _save_failure_streaks(self, streaks: Mapping[str, int]) -> None:
        heartbeat = _module("heartbeat")
        marker = self._failure_marker_path()
        try:
            marker.lstat()
        except FileNotFoundError:
            # A durable first-write witness makes subsequent history deletion fail closed.
            heartbeat._write_pinned(marker, {"schemaVersion": 1})
        else:
            try:
                if heartbeat._read_pinned(marker) != {"schemaVersion": 1}:
                    raise ValueError("untrusted witness")
            except (OSError, ValueError, UnicodeError):
                raise WorkerConfigError("worker failure history witness is untrusted") from None
        heartbeat._write_pinned(self._failure_state_path(), {"schemaVersion": 1, "streaks": dict(sorted(streaks.items()))})

    def _beat(self, now: int, report: Mapping[str, Any]) -> None:
        """Durable, payload-free health and alert-delivery failure tracking.

        An unavailable alert queue cannot silently turn a persistent worker
        incident into a healthy heartbeat. Persist streaks before publishing
        a new health claim; a crash may overcount, never erase a failure.
        """
        failed = {name for name, value in report.items() if isinstance(value, Mapping) and "failed" in value}
        streaks = dict(self._failure_streaks)
        for name in report:
            streaks[name] = min(3, streaks.get(name, 0) + 1) if name in failed else 0
        persistent = sorted(name for name, count in streaks.items() if count >= 3 and name != "alerts")
        try:
            self._failure_alerts(now, persistent, ran={name for name in report if name not in failed})
        except Exception:  # noqa: BLE001 - record the alert-path failure, never the sensitive exception
            failed.add("alerts")
            streaks["alerts"] = min(3, streaks.get("alerts", 0) + 1)
        else:
            if "alerts" in streaks:
                streaks["alerts"] = 0
        self._save_failure_streaks(streaks)
        document = {"at": now, "failed": sorted(failed),
                    "persistentFailures": sorted(name for name, count in streaks.items() if count >= 3)}
        _module("heartbeat")._write_pinned(self.heartbeat, document)
        self._failure_streaks = streaks

    def _failure_alerts(self, now: int, persistent: list[str], *, ran: set[str]) -> None:
        """One notice when a job starts failing three passes in a row and one when it recovers; state, not time, deduplicates flapping.

        The notice names only the job. A job that did not run this pass keeps its state, so a restart (which forgets the streaks) is not
        reported as a recovery. With nobody to tell, the state is still kept.
        """
        path = self.data / "worker" / "alerts.json"
        try:
            known = {item for item in json.loads(path.read_text(encoding="utf-8")).get("persistent", []) if isinstance(item, str)}
        except (OSError, ValueError, TypeError, AttributeError):
            known = set()
        current = (known - ran) | set(persistent)
        if current == known:
            return
        to = self.config["escalateTo"] or (self.config["mailbox"] or {}).get("owner")
        if to:
            for job in sorted(set(persistent) - known):
                self.queue.add("worker-failing", to, f"The worker job {job} has failed three passes in a row. Customers may be affected; an operator should check the "
                               "runtime (the ledger shows `worker.job-failed`).", reference=f"{job}:{now}"[:40])
            for job in sorted(known - current):
                self.queue.add("worker-recovered", to, f"The worker job {job} is working again.", reference=f"{job}:{now}"[:40])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"persistent": sorted(current)}) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def run_forever(self, stop: threading.Event, *, pause: int = MIN_INTERVAL) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(pause)


def operational_health(heartbeat: pathlib.Path, *, now: float, max_age: int = 300) -> dict[str, Any]:
    """Small, payload-free worker readiness signal, independent of Docker liveness.

    Three failed attempts at the same job mean degradation; a healthy heartbeat is
    still live so restarting the worker cannot obscure a provider outage.
    """
    return _module("heartbeat").check(heartbeat, now=now, max_age=max_age)


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
