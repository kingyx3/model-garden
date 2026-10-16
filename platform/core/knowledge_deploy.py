"""Ledger a change to the business rules a coworker may answer from, so a deploy cannot quietly change what it says.

The approved business-rules file is the coworker's authority for what it promises customers (hours, price boundaries,
escalation). Changing it needs a trail (#116): which source, which fields, which version and content digest, and who deployed
it. The governed sidecar calls ``record_deployment`` as it starts: it fingerprints the KnowledgeSources in its compiled
desired state (declared fields and content digest only, never the text), compares them with the fingerprint it last applied (a
small private file beside the ledger) and, when they differ, appends one chained ``knowledge.change`` operator event. An
unchanged restart records nothing.

- **Critical changes are named.** ``newlyApproved`` (a source became approved), ``approvedContentChanged`` (an approved source's
  text or version changed), ``audienceWidened`` (more coworkers, or every coworker, may now answer from an approved source) and
  ``retired`` are listed so a reviewer can find them in the trail. A draft being edited is recorded but is not critical, because
  it cannot ground an answer.
- **Ledger first, then remember.** The change is ledgered before the new fingerprint is stored, so a crash in between can only
  repeat an entry, never lose one; if the ledger cannot be written the call raises and the sidecar does not start.
- **First start.** With no earlier fingerprint every current source counts as added, so the trail begins with what the runtime
  started with.
- **Non-secret.** The event holds source names, field names, versions and the first 16 characters of content digests.
  It never holds rule text. The deployer identity is the one the deploy asserts (``policy_deploy.deployer``); it is not
  authenticated. An optional compile-time check (``approval_owner_errors``) can require an approved source's owner to be one
  of the environment's named ``governance.owners``, which narrows who may claim ownership but does not authenticate them.

Not done: checking that the content is still true, evals re-run before promotion, rollback, or the owner's own sign-off on the
change (a person still approves in the client repository).
"""
from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Callable, Mapping
from typing import Any

APPLIED_FILE = "knowledge-applied.json"
SCHEMA_VERSION = 1
ACTION = "knowledge.change"
FIELDS = ("type", "classification", "owner", "status", "version", "effectiveDate", "reviewBy", "audiences", "sha256")
Record = Callable[[dict[str, Any] | None, dict[str, Any], str], Mapping[str, Any] | None]


def fingerprint(desired_state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{source name: declared fields and content digest}`` for the KnowledgeSources the Agent selects."""
    content = desired_state.get("knowledgeContent")
    content = content if isinstance(content, Mapping) else {}
    result: dict[str, dict[str, Any]] = {}
    for source in desired_state.get("knowledge") or []:
        if not isinstance(source, Mapping):
            continue
        metadata, spec = source.get("metadata") or {}, source.get("spec") or {}
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            continue
        entry: dict[str, Any] = {"type": spec.get("type"), "classification": spec.get("classification"), "owner": metadata.get("owner")}
        if spec.get("type") == "text-file":
            entry.update(
                status=spec.get("status", "draft"), version=spec.get("version"), effectiveDate=spec.get("effectiveDate"),
                reviewBy=spec.get("reviewBy"),
                audiences=sorted(spec["audiences"]) if isinstance(spec.get("audiences"), list) else None,
            )
            digest = (content.get(name) or {}).get("sha256") if isinstance(content.get(name), Mapping) else None
            if isinstance(digest, str):
                entry["sha256"] = digest
        result[name] = {key: value for key, value in entry.items() if value is not None}
    return result


def _widened(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool:
    """Whether more coworkers may answer from the source than before (no audience list means every selecting coworker)."""
    old, new = (before or {}).get("audiences"), after.get("audiences")
    if new is None:
        return old is not None or before is None
    return old is not None and bool(set(new) - set(old))


def diff(before: Mapping[str, Mapping[str, Any]] | None, after: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """What changed between two fingerprints, with the critical changes named. Non-secret."""
    old = before or {}
    added, removed = sorted(set(after) - set(old)), sorted(set(old) - set(after))
    changed: dict[str, list[str]] = {}
    for name in sorted(set(old) & set(after)):
        fields = [field for field in FIELDS if old[name].get(field) != after[name].get(field)]
        if fields:
            changed[name] = fields
    newly_approved = sorted(
        name for name in [*added, *changed] if after[name].get("status") == "approved" and (old.get(name) or {}).get("status") != "approved"
    )
    content_changed = sorted(
        name for name, fields in changed.items()
        if after[name].get("status") == "approved" and old[name].get("status") == "approved" and ({"sha256", "version"} & set(fields))
    )
    widened = sorted(
        name for name in [*added, *changed]
        if after[name].get("status") == "approved" and _widened(old.get(name), after[name])
        and name not in newly_approved
    )
    retired = sorted(name for name in changed if after[name].get("status") == "retired" and old[name].get("status") != "retired")
    revisions = {
        name: {"from": {k: old[name].get(k) for k in ("version", "sha256") if old[name].get(k)},
               "to": {k: after[name].get(k) for k in ("version", "sha256") if after[name].get(k)}}
        for name in changed if {"version", "sha256"} & set(changed[name])
    }
    for revision in revisions.values():
        for side in revision.values():
            if "sha256" in side:
                side["sha256"] = side["sha256"][:16]
    return {
        "added": added, "removed": removed, "changed": changed, "newlyApproved": newly_approved,
        "approvedContentChanged": content_changed, "audienceWidened": widened, "retired": retired,
        "revisions": revisions, "sources": len(after),
    }


def has_change(summary: Mapping[str, Any]) -> bool:
    return bool(summary["added"] or summary["removed"] or summary["changed"])


def record_change(govern: Any, audit_path: pathlib.Path | None, before: dict[str, Any] | None, after: dict[str, Any], changed_by: str):
    """Append the ``knowledge.change`` operator event (``govern`` is ``govern-action.py``); ``None`` when nothing changed."""
    summary = diff(before, after)
    return govern.record_operator_action(audit_path, ACTION, changed_by, summary) if has_change(summary) else None


def _load_applied(path: pathlib.Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, "none"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or not isinstance(loaded.get("sources"), dict):
        return None, "unreadable"
    return loaded["sources"], "recorded"


def _store(path: pathlib.Path, sources: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"schemaVersion": SCHEMA_VERSION, "sources": sources}, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def record_deployment(sources: Mapping[str, Any], root: pathlib.Path, *, changed_by: str, record: Record) -> dict[str, Any]:
    """Ledger the difference between the sources last applied under ``root`` and ``sources``, then remember ``sources``.

    ``record(before, after, changed_by)`` appends the event and returns it, or ``None`` when nothing differs. Raises if the
    event cannot be recorded; nothing is stored then.
    """
    after = {name: dict(entry) for name, entry in sources.items()}
    path = pathlib.Path(root) / APPLIED_FILE
    before, baseline = _load_applied(path)
    event = record(before, after, changed_by)
    if baseline != "recorded" or event is not None:
        _store(path, after)
    details = (event or {}).get("details") or {}
    return {
        "changed": event is not None, "baseline": baseline, "changedBy": changed_by,
        "critical": sorted({*details.get("newlyApproved", []), *details.get("approvedContentChanged", []), *details.get("audienceWidened", [])}),
    }


def approval_owner_errors(sources: Any, trusted_owners: list[str] | None) -> list[str]:
    """Errors for approved text sources whose owner is not one of the environment's named owners (when it names any)."""
    if trusted_owners is None:
        return []
    errors = []
    for source in sources or []:
        if not isinstance(source, Mapping) or (source.get("spec") or {}).get("type") != "text-file":
            continue
        if (source.get("spec") or {}).get("status") != "approved":
            continue
        name, owner = (source.get("metadata") or {}).get("name"), str((source.get("metadata") or {}).get("owner", "")).strip().lower()
        if owner not in trusted_owners:
            errors.append(
                f"KnowledgeSource {name!r} is approved but its owner {owner!r} is not one of the environment's governance.owners"
            )
    return errors
