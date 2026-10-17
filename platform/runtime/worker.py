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
- **retention**: purges expired artifacts and mail past the retention period.

A failing job is ledgered as ``worker.job-failed`` with the job name and error type only and never stops the other jobs. A
heartbeat file records the last pass for the container health check. The worker holds no provider credential and no model
credential; the only secret it reads is the Hermes API server key (``MODEL_GARDEN_COWORKER_API_KEY``), and only when a coworker
URL is configured.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_INTERVALS = {"scheduler": 60, "mailbox": 30, "notices": 60, "retention": 3600}
MIN_INTERVAL, MAX_INTERVAL = 10, 86_400
MAX_QUEUE = 2_000
IDENTITY = "runtime:worker"


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _module(key: str):
    paths = {
        "govern": "scripts/govern-action.py",
        "schedules": "platform/core/schedules.py",
        "scheduler": "platform/core/scheduler.py",
        "delivery": "platform/core/notice_delivery.py",
        "notices": "platform/core/connection_notices.py",
        "capabilities": "platform/core/capabilities.py",
        "grants": "platform/core/provider_grants.py",
        "artifacts": "platform/core/artifacts.py",
        "spool": "platform/channels/mail_spool.py",
        "support": "platform/channels/support_mailbox.py",
        "email": "platform/channels/email_channel.py",
        "text": "platform/channels/text_channel.py",
        "coworker": "platform/runtime/coworker_client.py",
        "sidecar": "platform/runtime/governed_mcp.py",
    }
    return _load(f"model_garden_worker_{key}", paths[key])


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
    allowed = {"deployedBy", "intervals", "people", "escalateTo", "owners", "mailbox", "coworker", "connectors", "artifactRetentionDays"}
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
        self.queue = NoticeQueue(self.data / "worker")
        self.last_run: dict[str, float] = {}
        self.heartbeat = self.data / "worker" / "heartbeat.json"
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
        report = self._schedule_store().reconcile(definitions, by=by, now=now)
        return {"applied": True, **{key: len(value) for key, value in report.items()}}

    def run_scheduler(self, now: int) -> dict[str, Any]:
        if self.coworker is None:
            return {"skipped": "no-coworker-configured"}
        scheduler = _module("scheduler").Scheduler(
            self._schedule_store(), _module("coworker").schedule_runner(self.coworker),
            grants_for=self.grants_for, permissions_for=self.permissions_for, handoff_root=self.data / "handoffs",
            audit=self.audit,
            notify=lambda notice: self.queue.add(notice["kind"], notice.get("to"), notice["text"], reference=str(notice.get("taskId", ""))[:40]),
        )
        due = scheduler.run_due(now=now)
        resumed = scheduler.resume_waiting(now=now)
        return {"runs": len(due["runs"]), "resumed": len(resumed["runs"])}

    def _sidecar(self):
        environ = {
            **os.environ, "MODEL_GARDEN_INITIATING_IDENTITY": "runtime:support-mailbox",
            "MODEL_GARDEN_CAPABILITY_MANIFEST": os.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST", ""),
        }
        return _module("sidecar").GovernedToolRuntime(
            self.desired_state_path, self.audit_path, self.data / "approvals", environ=environ,
        )

    def _artifacts(self):
        days = self.config.get("artifactRetentionDays")
        options = {"retention_days": days} if isinstance(days, int) else {}
        return _module("artifacts").ArtifactStore(self.data / "artifacts", tenant=self.tenant, audit=self.audit, **options)

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

        mailbox = support.SupportMailbox(
            adapter, text.PrincipalDirectory(), {"mail.draft": draft}, classify=classify, answer=answer, escalate=escalate,
            handled=support.MessageLog(self.data / "mail-handled"), audit=self.audit,
        )
        return lambda raw, now: mailbox.handle(raw, now=now)

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
        results = delivery.deliver(sources, directory, {"inbox": inbox_sender, **self.senders}, audit=self.audit)
        counts: dict[str, int] = {}
        for result in results:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        return counts

    def run_retention(self, now: int) -> dict[str, Any]:
        out: dict[str, Any] = {"artifacts": self._artifacts().purge_expired(now=now)}
        if self.config["mailbox"] is not None:
            out["mail"] = self._spool().purge(now=now)
        return out

    # -- loop ------------------------------------------------------------------------------------------

    def jobs(self) -> list[tuple[str, Callable[[int], dict[str, Any]]]]:
        return [
            ("scheduler", self.run_scheduler), ("mailbox", self.run_mailbox),
            ("notices", self.deliver_notices), ("retention", self.run_retention),
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
        failed = sorted(name for name, value in report.items() if isinstance(value, Mapping) and "failed" in value)
        temporary = self.heartbeat.with_suffix(".tmp")
        temporary.write_text(json.dumps({"at": now, "failed": failed}) + "\n", encoding="utf-8")
        os.replace(temporary, self.heartbeat)

    def run_forever(self, stop: threading.Event, *, pause: int = MIN_INTERVAL) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(pause)


def healthy(heartbeat: pathlib.Path, *, now: float, max_age: int = 300) -> bool:
    try:
        return now - json.loads(heartbeat.read_text(encoding="utf-8"))["at"] <= max_age
    except (OSError, ValueError, KeyError, TypeError):
        return False


def build(args: argparse.Namespace, environ: Mapping[str, str]) -> Worker:
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config and args.config.is_file() else {}
    if not config.get("deployedBy") and environ.get("MODEL_GARDEN_DEPLOYED_BY", "").strip():
        config["deployedBy"] = environ["MODEL_GARDEN_DEPLOYED_BY"].strip()
    coworker = None
    settings = config.get("coworker")
    if settings:
        key = environ.get("MODEL_GARDEN_COWORKER_API_KEY", "")
        coworker = _module("coworker").CoworkerClient(settings["url"], key, model=settings.get("model", "hermes-agent"))
    return Worker(args.desired_state, args.data_root, config, coworker=coworker)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--desired-state", type=pathlib.Path,
                        default=pathlib.Path(os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--data-root", type=pathlib.Path, default=pathlib.Path(os.environ.get("MODEL_GARDEN_DATA_ROOT", "/opt/data/.modelgarden")))
    parser.add_argument("--config", type=pathlib.Path,
                        default=pathlib.Path(os.environ.get("MODEL_GARDEN_WORKER_CONFIG", "/opt/profile-seed/.modelgarden/worker.json")))
    parser.add_argument("--once", action="store_true", help="run one pass and print what each job did")
    parser.add_argument("--health", action="store_true", help="exit 0 when the last pass is recent (container health check)")
    args = parser.parse_args(argv)
    if args.health:
        return 0 if healthy(args.data_root / "worker" / "heartbeat.json", now=time.time()) else 1
    try:
        worker = build(args, os.environ)
    except (OSError, ValueError, KeyError) as exc:
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
