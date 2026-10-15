"""Ledger a Policy change when a deployment applies it, so a deploy cannot change policy without a trace.

``record_policy_change`` (``scripts/govern-action.py``) can ledger who changed the client's Policy rules and the rule-level diff,
but nothing called it (#114). The governed sidecar now calls ``record_deployment`` as it starts: it compares the Policy rules in the
compiled desired state it was deployed with against the rules it last applied (kept in a small private file beside the ledger)
and, when they differ, appends one chained ``policy-change`` event naming who deployed it, which rules were added, removed or
modified and which of them loosen control. An unchanged restart records nothing.

- **Ledger first, then remember.** The change is ledgered before the new rules are stored as applied, so a crash in between can only
  repeat an entry, never lose one. If the ledger cannot be written the call raises and the sidecar does not start with a policy it
  could not account for.
- **Who.** ``MODEL_GARDEN_DEPLOYED_BY`` (a named person or workflow identity) when the deploy sets it, else ``runtime:deployment``.
  It is asserted by whoever runs the deploy, not authenticated: it names the person in the trail but does not prove who they are,
  and it does not replace the owner sign-off on relaxing rules (checked at compile time and by the runtime).
- **First start.** With no record of earlier rules (a first deployment, or an upgrade from a platform release that did not keep
  one) every current rule counts as added, so the trail begins with the rules the runtime started with.
- **Non-secret.** The applied file holds the Policy resources already present in the compiled desired state, and the event holds
  rule ids and versions only; neither holds a credential or any customer content.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from collections.abc import Callable, Mapping
from typing import Any

APPLIED_FILE = "policy-applied.json"
SCHEMA_VERSION = 1
DEFAULT_IDENTITY = "runtime:deployment"
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
Record = Callable[[list[dict[str, Any]] | None, list[dict[str, Any]] | None, str], Mapping[str, Any] | None]


def deployer(environ: Mapping[str, str] | None = None) -> str:
    """The identity to ledger as having deployed this policy."""
    value = (os.environ if environ is None else environ).get("MODEL_GARDEN_DEPLOYED_BY", "").strip()
    return value if _PERSON.match(value) else DEFAULT_IDENTITY


def _policies(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _load_applied(path: pathlib.Path) -> tuple[list[dict[str, Any]] | None, str]:
    if not path.is_file():
        return None, "none"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or not isinstance(loaded.get("policies"), list):
        return None, "unreadable"
    return _policies(loaded["policies"]), "recorded"


def _store(path: pathlib.Path, policies: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"schemaVersion": SCHEMA_VERSION, "policies": policies}, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def record_deployment(
    policies: Any,
    root: pathlib.Path,
    *,
    changed_by: str,
    record: Record,
) -> dict[str, Any]:
    """Ledger the difference between the rules last applied under ``root`` and ``policies``, then remember ``policies``.

    ``record(before, after, changed_by)`` appends the ledger event and returns it, or ``None`` when nothing differs (for example
    ``govern-action.py``'s ``record_policy_change``). Raises if the event cannot be recorded; nothing is stored then.
    """
    after = _policies(policies)
    path = pathlib.Path(root) / APPLIED_FILE
    before, baseline = _load_applied(path)
    event = record(before, after, changed_by)
    if baseline != "recorded" or event is not None:
        _store(path, after)
    details = (event or {}).get("details") or {}
    return {
        "changed": event is not None, "baseline": baseline, "changedBy": changed_by,
        "previousVersion": details.get("previousVersion"), "newVersion": details.get("newVersion"),
        "relaxed": list(details.get("relaxed") or []),
    }
