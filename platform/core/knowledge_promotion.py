"""Promote a change to the approved business rules only when evals that ran against that exact content passed, with a way back (#116).

``knowledge_deploy.py`` ledgers a change to the rules a coworker answers from after the fact. This is the gate in front of it: a
*critical* change must not reach production until the acceptance evals have been run against the very text being promoted, and a
bad promotion must be undoable to a known earlier state. It is pure and credential-free: it reads compiled desired-state documents
and JSON records, writes nothing itself and holds no secret. The promotion record is meant to live in the client's workspace
repository, where it is reviewed like any other change; ``scripts/knowledge-promotion.py`` is the command a CI job or an operator runs.

- **What is critical.** The same changes ``knowledge_deploy.diff`` names: a source becoming approved, an approved source whose text or
  version changed, and an approved source whose audience widened. A draft being edited, a retirement and the removal of a source make
  a coworker say *less*, so they pass without evals.
- **Evidence is bound to the content, not to a name.** An eval run records, for each source it ran against, the version and the SHA-256
  of the text. A critical source is covered only when the evidence holds the same digest and version as the content being promoted, so
  evals run against yesterday's text cannot promote today's. Evidence must say it passed, name the expected suite, be recent (default
  seven days) and not come from the future.
- **A named owner promotes.** When the environment names its ``governance.owners``, the person promoting must be one of them. This
  narrows who may claim to promote; it does not authenticate them, and the record holds the identity as asserted.
- **A record, not a ledger.** The record keeps the promoted snapshot (declared fields and content digests, never text), who promoted it
  and when, and a digest of the evidence that justified it, plus the snapshot promoted before. ``verify`` compares what is about to be
  deployed with the promoted snapshot, so content that changed *without* a promotion is caught as drift.
- **Rollback is to a known state.** ``rollback`` makes the previous snapshot the promoted one (recording what it rolled back from), and
  ``rollback_plan`` lists the sources, versions and digests to restore in the workspace. Restoring the text is a repository action
  (the record cannot hold it, by design), after which ``verify`` proves the restored content is the one that was promoted.
- **Not done here.** Whether the rules are *true*, authenticating the promoter, external knowledge providers and a live eval that calls
  a model are not part of this; the eval is the credential-free reference suite.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import sys
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_SUITE = "receptionist"
DEFAULT_MAX_AGE_SECONDS = 7 * 86400
CRITICAL_KEYS = ("newlyApproved", "approvedContentChanged", "audienceWidened")
MAX_NAME = 128


class PromotionError(ValueError):
    """Raised for a malformed record or evidence document."""


class PromotionBlocked(PermissionError):
    """Raised by :func:`promote` when the gate says no; ``reasons`` holds the stable codes."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("promotion blocked: " + ", ".join(reasons))
        self.reasons = reasons


def _deploy():
    name = "model_garden_promotion_knowledge_deploy"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("knowledge_deploy.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def evidence_digest(evidence: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(evidence).encode("utf-8")).hexdigest()


def _iso(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
    except ValueError:
        return None


# -- evidence -----------------------------------------------------------------------------


def build_evidence(desired_state: Mapping[str, Any], *, suite: str, passed: bool, failures: int, now: int) -> dict[str, Any]:
    """The evidence an eval run leaves: the suite, the result and the version and digest of every knowledge source it ran against."""
    sources = {
        name: {key: entry[key] for key in ("version", "sha256", "status") if key in entry}
        for name, entry in _deploy().fingerprint(desired_state).items() if entry.get("type") == "text-file"
    }
    return {"schemaVersion": SCHEMA_VERSION, "suite": suite, "passed": bool(passed), "failures": int(failures),
            "ranAt": _iso(now), "knowledge": sources}


# -- the gate -----------------------------------------------------------------------------


def _sources(record: Mapping[str, Any] | None, key: str) -> dict[str, Any] | None:
    section = (record or {}).get(key)
    if section is None:
        return None
    if not isinstance(section, Mapping) or not isinstance(section.get("sources"), Mapping):
        raise PromotionError(f"the promotion record's {key!r} section is malformed")
    return dict(section["sources"])


def check(
    desired_state: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
    *,
    now: int,
    promoted_by: str | None = None,
    owners: Iterable[str] | None = None,
    suite: str = DEFAULT_SUITE,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Whether ``desired_state`` may be promoted over ``record`` (or over nothing, for a first promotion).

    ``allowed`` is true when no critical change needs evidence or when valid evidence covers every critical source. ``reasons`` are
    stable codes, never rule text. Nothing is raised for a blocked change; :func:`promote` raises.
    """
    current = _deploy().fingerprint(desired_state)
    summary = _deploy().diff(_sources(record, "promoted"), current)
    critical = sorted({name for key in CRITICAL_KEYS for name in summary[key]})
    reasons: list[str] = []
    if critical:
        if owners is not None and (not isinstance(promoted_by, str) or promoted_by.strip().lower() not in {str(o).strip().lower() for o in owners}):
            reasons.append("promoter-not-an-owner")
        if not isinstance(evidence, Mapping):
            reasons.append("no-evidence")
        else:
            if evidence.get("suite") != suite:
                reasons.append("evidence-wrong-suite")
            if evidence.get("passed") is not True:
                reasons.append("evidence-not-passing")
            ran = _epoch(evidence.get("ranAt"))
            if ran is None:
                reasons.append("evidence-undated")
            elif ran > now + 60:
                reasons.append("evidence-from-the-future")
            elif now - ran > max_age_seconds:
                reasons.append("evidence-stale")
            covered = evidence.get("knowledge") if isinstance(evidence.get("knowledge"), Mapping) else {}
            for name in critical:
                seen = covered.get(name) if isinstance(covered.get(name), Mapping) else {}
                if not seen or seen.get("sha256") != current[name].get("sha256") or seen.get("version") != current[name].get("version"):
                    reasons.append(f"evidence-not-for-this-content:{name[:MAX_NAME]}")
    return {"allowed": not reasons, "critical": critical, "reasons": reasons, "summary": summary}


def promote(
    desired_state: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
    *,
    by: str,
    now: int,
    owners: Iterable[str] | None = None,
    suite: str = DEFAULT_SUITE,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """The new promotion record, or :class:`PromotionBlocked`. The previous promoted snapshot is kept for rollback."""
    if not isinstance(by, str) or not by.strip() or len(by.strip()) > 128:
        raise PromotionError("promotion needs the named person promoting it")
    result = check(desired_state, record, evidence, now=now, promoted_by=by, owners=owners, suite=suite, max_age_seconds=max_age_seconds)
    if not result["allowed"]:
        raise PromotionBlocked(result["reasons"])
    previous = (record or {}).get("promoted")
    promoted: dict[str, Any] = {"sources": _deploy().fingerprint(desired_state), "at": _iso(now), "by": by.strip()}
    if result["critical"] and isinstance(evidence, Mapping):
        promoted["evidence"] = {"suite": evidence.get("suite"), "ranAt": evidence.get("ranAt"), "sha256": evidence_digest(evidence)}
    return {"schemaVersion": SCHEMA_VERSION, "promoted": promoted, "previous": previous, "history": _history(record, "promoted", by, now, result["critical"])}


def _history(record: Mapping[str, Any] | None, action: str, by: str, now: int, critical: list[str]) -> list[dict[str, Any]]:
    entries = list((record or {}).get("history") or [])
    entries.append({"action": action, "by": by.strip(), "at": _iso(now), "critical": critical})
    return entries[-50:]


# -- drift and rollback --------------------------------------------------------------------


def verify(desired_state: Mapping[str, Any], record: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether what is about to be deployed is exactly what was promoted. Content changed outside a promotion is drift."""
    promoted = _sources(record, "promoted")
    current = _deploy().fingerprint(desired_state)
    if promoted is None:
        return {"matches": not current, "unpromoted": sorted(current), "drifted": [], "missing": []}
    drifted = sorted(name for name in set(promoted) & set(current)
                     if any(promoted[name].get(key) != current[name].get(key) for key in ("status", "version", "sha256", "audiences")))
    return {"matches": not (set(current) - set(promoted)) and not (set(promoted) - set(current)) and not drifted,
            "unpromoted": sorted(set(current) - set(promoted)), "drifted": drifted, "missing": sorted(set(promoted) - set(current))}


def rollback(record: Mapping[str, Any] | None, *, by: str, now: int) -> dict[str, Any]:
    """Make the previous promoted snapshot the promoted one again; the snapshot rolled back from is kept so a rollback can be undone."""
    if not isinstance(by, str) or not by.strip() or len(by.strip()) > 128:
        raise PromotionError("rollback needs the named person deciding it")
    previous = (record or {}).get("previous")
    if not isinstance(previous, Mapping) or not isinstance(previous.get("sources"), Mapping):
        raise PromotionError("there is no earlier promotion to roll back to")
    return {"schemaVersion": SCHEMA_VERSION, "promoted": dict(previous), "previous": (record or {}).get("promoted"),
            "history": _history(record, "rolled-back", by, now, [])}


def rollback_plan(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """What to restore in the workspace to return to the previous promoted snapshot (names, versions, short digests only)."""
    previous = _sources(record, "previous")
    current = _sources(record, "promoted") or {}
    if previous is None:
        raise PromotionError("there is no earlier promotion to roll back to")
    steps = []
    for name in sorted(set(previous) | set(current)):
        old, new = previous.get(name), current.get(name)
        if old == new:
            continue
        steps.append({
            "source": name,
            "action": "remove" if old is None else ("restore" if new is not None else "re-add"),
            **({"version": old.get("version"), "sha256": old["sha256"][:16]} if old and old.get("sha256") else {}),
            **({"status": old.get("status")} if old else {}),
        })
    return steps
