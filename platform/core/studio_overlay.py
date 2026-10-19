"""What the live coworker takes from an approved Studio change without a redeploy (#130, #66, #86).

The Studio writes an overlay (``<data root>/studio/overlay.json``) when the owner approves a change. The gateway, the governed sidecar
and the worker share that data volume, so the two processes that act can read it. They take it up under one rule: **the live
runtime applies an approved change on its own only where it can narrow what the coworker may do, or change when and how much it
runs; everything that widens authority still goes through the workspace pull request** (``ChangeStore.export_patch``).

- **Approval policy (sidecar).** A rule from the overlay applies live only when its effect is at least as strict as what it replaces
  (``allow`` < ``require-approval`` < ``human-only`` < ``deny``) or it is a new non-``allow`` rule. An ``allow`` rule, a removal or a
  weaker replacement is *held*: it stays in the overlay and the workspace patch, and the sidecar ignores it. The sidecar holds no
  owner key, so it could not verify the owner's signature on a relaxing rule; this keeps a damaged or tampered overlay unable to
  loosen anything.
- **Schedule edits (worker).** The cron expression and per-run budget the owner approved are written onto that schedule, validated
  again with the store's own rules, and ledgered. A schedule the workspace manages keeps the edit across the worker's reconcile.
- **Not applied live.** Switching a Skill on or off is compiled into the Hermes profile, so it waits for the next deploy; new
  coworkers, knowledge promotion and roles are exported only.

Pure over dictionaries except :func:`read` and :class:`LiveOverlay`, which read one file; no credentials.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 1
OVERLAY_PATH = ("studio", "overlay.json")
MAX_BYTES = 1_000_000
STRENGTH = {"allow": 0, "require-approval": 1, "human-only": 2, "deny": 3}
POLICY_NAME = "studio-overlay"


def read(data_root: pathlib.Path) -> dict[str, Any]:
    """The overlay, or an empty one when it is missing, unreadable, oversized or of another version (fail to the workspace)."""
    path = pathlib.Path(data_root).joinpath(*OVERLAY_PATH)
    try:
        if path.stat().st_size > MAX_BYTES:
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) and loaded.get("schemaVersion") == SCHEMA_VERSION else {}


def _rule_ok(rule: Any) -> bool:
    if not isinstance(rule, Mapping) or not isinstance(rule.get("id"), str) or rule.get("effect") not in STRENGTH:
        return False
    if rule["effect"] == "human-only" and not (isinstance(rule.get("assignee"), str) and rule["assignee"].strip()):
        return False
    return rule["effect"] == "allow" or rule.get("conditions") is None


def effective_policies(policies: Any, overlay: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The Policy resources with the overlay's tightening rules applied, and the ids of the rules held for the workspace change."""
    resources = [copy.deepcopy(item) for item in policies or [] if isinstance(item, Mapping)]
    changes = overlay.get("policyRules")
    held: list[str] = []
    added: list[dict[str, Any]] = []
    if not isinstance(changes, Mapping):
        return resources, held
    existing = {rule.get("id"): rule for resource in resources for rule in (resource.get("spec") or {}).get("rules", []) if isinstance(rule, Mapping)}
    for rule_id, replacement in sorted(changes.items()):
        if replacement is None or not _rule_ok(replacement) or replacement["id"] != rule_id:
            held.append(str(rule_id))
            continue
        current = existing.get(rule_id)
        if current is None:
            if replacement["effect"] == "allow":
                held.append(rule_id)
            else:
                added.append(dict(replacement))
            continue
        if STRENGTH.get(current.get("effect"), 0) > STRENGTH[replacement["effect"]] or replacement["effect"] == "allow":
            held.append(rule_id)
            continue
        current.clear()
        current.update({key: value for key, value in replacement.items() if key != "signoff"})
    if added:
        resources.append({"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": POLICY_NAME}, "spec": {"rules": added}})
    return resources, held


def apply_to_state(state: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """The compiled desired state with the live-safe part of the overlay applied. The input is never changed."""
    if not overlay or not overlay.get("policyRules"):
        return dict(state)
    policies, _held = effective_policies(state.get("policies"), overlay)
    return {**state, "policies": policies}


def held_rules(state: Mapping[str, Any], overlay: Mapping[str, Any]) -> list[str]:
    return effective_policies(state.get("policies"), overlay)[1]


class LiveOverlay:
    """Reads the overlay when its file changes and hands back the effective state, so a request costs one ``stat``."""

    def __init__(self, data_root: pathlib.Path) -> None:
        self.path = pathlib.Path(data_root).joinpath(*OVERLAY_PATH)
        self._signature: tuple[int, int] | None = None
        self._base: Any = None
        self._effective: dict[str, Any] | None = None

    def _stamp(self) -> tuple[int, int] | None:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def apply(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        stamp = self._stamp()
        if stamp is None:
            self._signature, self._effective = None, None
            return state
        if stamp != self._signature or self._base is not state or self._effective is None:
            self._effective = apply_to_state(state, read(self.path.parents[1]))
            self._signature, self._base = stamp, state
        return self._effective
