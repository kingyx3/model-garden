"""Turn ledger-export verification into owner alerts that fire once, remind, and say when it is fixed (#80, #72).

``ledger_export.verify_export`` already tells a person whether the exported audit trail is intact and current. Nothing told anyone
when it stopped being so. This is the credential-free monitor: run it on a timer, it verifies the export, classifies what is
wrong, and produces notices for the named owner in the shape ``notice_delivery`` delivers through that person's own channel. It
sends nothing, schedules nothing itself and is not wired to a timer or the sidecar.

- **Three kinds of condition.** ``export-integrity`` (a broken or truncated chain, a gap, a local ledger that differs from the export,
  a missing or mismatched head manifest, an unreadable local ledger), ``export-lag`` (the local ledger is more events ahead than the
  limit) and ``export-stale`` (events are waiting and the newest head manifest is too old or was never exported). An idle,
  fully exported ledger raises none, and an exporter that was never configured raises nothing because there is nothing to compare.
- **Once, then reminders, then recovery.** A condition is alerted when it starts, again only after ``reminder_seconds`` while it
  persists (default six hours), and a single ``export-recovered`` notice is produced when every condition has cleared. State is kept
  in a private file so a restart does not repeat an alert; a notice that was not delivered is offered again (at least once).
- **Safe text.** A notice names the condition, the number of events behind where known and a recovery step. It never carries a
  ledger event, an exported record, a sequence's content or a path.
- **Unowned is loud.** With no owner configured, notices carry ``to: None`` so ``notice_delivery`` reports them ``unowned`` on every
  run; an alert is never dropped for want of an address.
- **Integrity outranks everything.** An integrity problem is reported as its own condition even when lag or staleness are also present,
  and its text says the export cannot be trusted until a person checks it.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_REMINDER_SECONDS = 6 * 3600
CONDITIONS = ("export-integrity", "export-lag", "export-stale")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
Audit = Callable[[str, str, dict[str, Any]], Any]
_ACTIONS = {
    "export-integrity": "the exported audit trail cannot be trusted until a person checks it: run the ledger export verifier, compare with the local ledger and restore from a known-good copy if needed",
    "export-lag": "the exporter is falling behind: check that the export job is running and can reach its sink",
    "export-stale": "events are waiting and the export has not run recently: check the export job and its credentials, then run it",
}


def classify(problem: str) -> str:
    """Which condition one ``verify_export`` problem line belongs to."""
    if problem.startswith("export lag"):
        return "export-lag"
    if problem.startswith("export stale"):
        return "export-stale"
    return "export-integrity"


class ExportMonitor:
    def __init__(self, root: pathlib.Path, *, name: str = "ledger", audit: Audit | None = None) -> None:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
            raise ValueError("name is a short lowercase word identifying this export")
        self.name = name
        self.root = pathlib.Path(root)
        self.path = self.root / f"{name}.export-monitor.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / f".{self.name}.export-monitor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "conditions": {}, "recovery": None}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise ValueError("export monitor state has an unsupported format")
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

    def _record(self, action: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, "runtime:export-monitor", details)

    def evaluate(
        self,
        report: Mapping[str, Any],
        *,
        now: int,
        owner: str | None,
        escalate_to: str | None = None,
        reminder_seconds: int = DEFAULT_REMINDER_SECONDS,
    ) -> list[dict[str, Any]]:
        """Notices due now for a ``verify_export`` report, updating the monitor's state.

        Each notice is ``{"key", "kind", "to", "text"}``; a notice is offered again until ``mark_notified`` records its delivery.
        """
        for label, value in (("owner", owner), ("escalate_to", escalate_to)):
            if value is not None and (not isinstance(value, str) or not _PERSON.match(value)):
                raise ValueError(f"{label} must be a named person")
        if not isinstance(reminder_seconds, int) or isinstance(reminder_seconds, bool) or reminder_seconds < 60:
            raise ValueError("reminder_seconds is at least 60")
        active: dict[str, list[str]] = {}
        for problem in report.get("problems", []):
            active.setdefault(classify(str(problem)), []).append(str(problem))
        lag = int(report.get("lagEvents") or 0)
        notices: list[dict[str, Any]] = []
        started: list[str] = []
        with self._locked() as state:
            conditions = state["conditions"]
            for kind in CONDITIONS:
                if kind in active:
                    entry = conditions.get(kind)
                    if entry is None:
                        entry = conditions[kind] = {"since": now, "lastNotifiedAt": None}
                        started.append(kind)
                    due = entry["lastNotifiedAt"] is None or now - entry["lastNotifiedAt"] >= reminder_seconds
                    if due:
                        behind = f" ({lag} event(s) behind)" if kind in ("export-lag", "export-stale") and lag else ""
                        again = " This is a reminder." if entry["lastNotifiedAt"] is not None else ""
                        notices.append({
                            "key": hashlib.sha256(f"{self.name}\0{kind}\0{entry['since']}\0{entry['lastNotifiedAt']}".encode("utf-8")).hexdigest(),
                            "kind": kind, "to": owner, "condition": kind,
                            "text": f"Ledger export {kind.removeprefix('export-')} problem{behind}: {_ACTIONS[kind]}.{again}",
                        })
                elif kind in conditions:
                    del conditions[kind]
                    state["recovery"] = {"at": now, "notifiedAt": None}
            if not conditions and state.get("recovery") and state["recovery"]["notifiedAt"] is None:
                recovery = state["recovery"]
                notices.append({
                    "key": hashlib.sha256(f"{self.name}\0recovered\0{recovery['at']}".encode("utf-8")).hexdigest(),
                    "kind": "export-recovered", "to": owner or escalate_to, "condition": "export-recovered",
                    "text": "Ledger export is healthy again: the exported audit trail is intact and current.",
                })
            elif conditions:
                state["recovery"] = None
        for kind in started:
            self._record("export.alert-started", {"condition": kind, "name": self.name})
        return notices

    def mark_notified(self, notice: Mapping[str, Any], *, via: str, now: int) -> None:
        """Record that a notice reached a person, so it is not offered again until the reminder interval (or never, for a recovery)."""
        kind = notice.get("condition")
        if not isinstance(via, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", via):
            raise ValueError("name the delivery channel, for example slack or inbox")
        with self._locked() as state:
            if kind == "export-recovered":
                if state.get("recovery"):
                    state["recovery"]["notifiedAt"] = now
            elif kind in state["conditions"]:
                state["conditions"][kind]["lastNotifiedAt"] = now
            else:
                return
        self._record("export.alert-delivered", {"condition": kind, "name": self.name, "via": via})

    def check(
        self,
        ledger_path: pathlib.Path,
        sink: Any,
        *,
        now: int,
        owner: str | None,
        escalate_to: str | None = None,
        max_lag_events: int | None = 100,
        max_head_age_seconds: int | None = 6 * 3600,
        reminder_seconds: int = DEFAULT_REMINDER_SECONDS,
        verify: Callable[..., Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Verify the export and return ``{"report", "notices"}``; ``verify`` defaults to ``ledger_export.verify_export``."""
        if verify is None:
            import importlib.util
            import sys
            name = "model_garden_monitor_ledger_export"
            if name not in sys.modules:
                spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("ledger_export.py"))
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                sys.modules[name] = module
                spec.loader.exec_module(module)
            verify = sys.modules[name].verify_export
        report = dict(verify(ledger_path, sink, max_lag_events=max_lag_events, max_head_age_seconds=max_head_age_seconds, now=now))
        return {"report": report, "notices": self.evaluate(report, now=now, owner=owner, escalate_to=escalate_to, reminder_seconds=reminder_seconds)}
