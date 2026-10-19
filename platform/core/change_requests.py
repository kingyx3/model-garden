"""Change requests: the evidence-gated way an owner changes a coworker from the Studio or the host (#130, #66, #86, #110, #111).

An owner proposes one change (switch a Skill or Skillset on or off, edit a schedule, add/replace/remove an approval Policy rule,
promote changed business knowledge, or create a new coworker from an existing one). The proposal becomes a **change request**: a
JSON document under ``<data root>/studio/changes`` holding the exact diff, a digest of that content and an **evidence checklist**.
Nothing changes until the *named owner* explicitly approves that exact digest and every required evidence item is met when the
approval is made (evidence is recomputed then, never trusted from the proposal). Approval records a rollback snapshot and appends
ledger events. This module is pure of HTTP and holds no credential; ``platform/runtime/studio.py`` and
``scripts/studio-changes.py`` are the two front doors.

- **Evidence, per kind.** Enabling a Skill needs its readiness met (``capabilities`` / ``grant_readiness``: the coworker's own grant
  and connected dependencies); a Skill that reads business knowledge also needs ``knowledge_promotion`` evals that ran against the
  exact content. A schedule edit must validate (cron fires and is not too frequent, budget well formed). A Policy change that
  *relaxes* control needs the owner's Ed25519 sign-off (``policy.sign_rule`` / ``policy.signature_valid``) bound to the exact rule
  and verified against ``studio.ownerKeys``; with no key configured it fails closed. A new coworker starts with every Skill that
  needs its own access switched off and an owner-readable onboarding pack (``onboarding_pack``) that the owner approves by digest.
- **What "applied" means.** Applying writes the Studio *overlay* (``studio/overlay.json``) and keeps the rollback snapshot; it does
  not rewrite the client's workspace repository or restart the runtime. ``export_patch`` produces the workspace change (Agent
  ``disabledSkills``, Schedule, Policy rule with its sign-off, promotion record, new Agent resource + pack) that the operator
  commits through the normal pull-request flow. Nothing here silently mutates a live client.
- **Access.** An owner can give a signed-in colleague roles (``author``, ``approver``, ``provisioner``, ``data-viewer``, ``promoter``) or
  take them away, as an ``access`` change like any other (exact diff, approved by digest, rolled back from a snapshot). The owner's own
  roles are never changed here, and a role is granted only to someone on the web sign-in list. Of these, only ``author`` is enforced by
  the Studio itself (an author may *propose* any change except an ``access`` one; approving, rejecting and rolling back stay with the
  owner); the others are recorded and exported in the workspace patch for the operator to merge. A role never carries a credential.
- **Authority.** Only the configured owner (the verified web session or the host operator naming that owner) may approve,
  reject or roll back, and propose an ``access`` change; an ``author`` may propose the other kinds. On the host the identity is as asserted (like ``knowledge-promotion.py``); the owner's Ed25519 signature is
  what authenticates a relaxing Policy change.
- **Safe to retry.** The ledger event is written before state; an approval interrupted after the rollback snapshot is stored is
  completed, not repeated; rollback is allowed only for the latest applied change and restores the exact earlier overlay.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import secrets
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
KINDS = ("skill", "skillset", "schedule", "policy", "knowledge", "new-coworker", "access")
ACCESS_ROLES = {
    "author": "propose changes to the coworker in the Studio (only the owner can approve them)",
    "approver": "approve the coworker's actions in chat",
    "provisioner": "start provider connections for the coworker",
    "data-viewer": "view customer data the coworker handles",
    "promoter": "promote an approved change to the live coworker",
}
PROPOSAL_TTL = 7 * 86400
MAX_OPEN = 50
MAX_RULE_BYTES = 8192
MAX_TEXT = 200
_ID = re.compile(r"^cr-[0-9a-f]{12}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
Audit = Callable[[str, str, dict[str, Any]], None]


class ChangeError(ValueError):
    """The proposal or request is malformed or impossible; the message is safe to show."""


class ChangeRefused(PermissionError):
    """A gate said no. ``reasons`` are stable codes (``not-the-owner``, ``evidence:<id>``, ``stale-base`` ...)."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("refused: " + ", ".join(reasons))
        self.reasons = reasons


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parents[2] / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _mod(key: str):
    paths = {
        "policy": "platform/core/policy.py", "promotion": "platform/core/knowledge_promotion.py", "schedules": "platform/core/schedules.py",
        "skillsets": "platform/core/skillsets.py", "readiness": "platform/core/grant_readiness.py", "pack": "platform/core/onboarding_pack.py",
    }
    return _load(f"model_garden_change_requests_{key}", paths[key])


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _iso(now: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def empty_overlay() -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "version": 0, "lastChange": None, "agents": {}, "schedules": {}, "policyRules": {},
            "coworkers": {}, "knowledgePromotion": None, "access": {}}


def _text(value: Any, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT or (pattern and not pattern.match(value.strip())):
        raise ChangeError(f"{label} is missing or not valid")
    return value.strip()


def _item(item_id: str, label: str, met: bool, detail: str = "", required: bool = True) -> dict[str, Any]:
    return {"id": item_id, "label": label, "required": required, "met": bool(met), "detail": detail[:300]}


class ChangeStore:
    """Proposals, approvals and rollbacks for one deployed coworker, under ``<data_root>/studio``."""

    def __init__(
        self, data_root: pathlib.Path, *, tenant: str, desired_state: Mapping[str, Any], manifest: Mapping[str, Any] | None,
        owner: str, owner_keys: Mapping[str, str] | None = None, catalog: Mapping[str, Any] | None = None,
        schedules: Callable[[], list[dict[str, Any]]] | None = None, audit: Audit | None = None,
        clock: Callable[[], float] = time.time,
        people: Mapping[str, Iterable[str]] | None = None, signin: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.data = pathlib.Path(data_root)
        self.root = self.data / "studio"
        self.tenant = tenant
        self.desired = dict(desired_state)
        self.owner = _text(owner, "owner", pattern=_PERSON)
        self.owner_keys = {k.strip().lower(): v for k, v in (owner_keys or {}).items()}
        self.catalog = dict(catalog) if catalog is not None else self._read_json(self.root / "skillset-catalog.json") or {}
        self._manifest = dict(manifest) if isinstance(manifest, Mapping) else None
        self._schedules = schedules
        self._audit = audit
        self.clock = clock
        self._people = {person.strip().lower(): sorted(set(roles)) for person, roles in (people or {}).items()}
        self._signin = signin

    # -- storage ------------------------------------------------------------------------------------------

    @staticmethod
    def _read_json(path: pathlib.Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, path: pathlib.Path, document: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, sort_keys=True, indent=1) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def overlay(self) -> dict[str, Any]:
        loaded = self._read_json(self.root / "overlay.json")
        if isinstance(loaded, dict) and loaded.get("schemaVersion") == SCHEMA_VERSION:
            return {**empty_overlay(), **loaded}
        return empty_overlay()

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def _path(self, change_id: str) -> pathlib.Path:
        if not isinstance(change_id, str) or not _ID.match(change_id):
            raise ChangeError("that is not a change request id")
        return self.root / "changes" / f"{change_id}.json"

    def get(self, change_id: str) -> dict[str, Any]:
        document = self._read_json(self._path(change_id))
        if not isinstance(document, dict):
            raise ChangeError("no such change request")
        return document

    def list(self) -> list[dict[str, Any]]:
        folder = self.root / "changes"
        found = [self._read_json(path) for path in sorted(folder.glob("cr-*.json"))] if folder.is_dir() else []
        return sorted((d for d in found if isinstance(d, dict)), key=lambda d: (d.get("proposedAt", ""), d.get("id", "")), reverse=True)

    @staticmethod
    def summary(document: Mapping[str, Any]) -> dict[str, Any]:
        return {key: document.get(key) for key in ("id", "kind", "agent", "state", "summary", "proposedBy", "proposedAt", "digest")}

    # -- the deployed coworker ----------------------------------------------------------------------------

    @property
    def agent(self) -> str:
        return str((self.desired.get("agent") or {}).get("metadata", {}).get("name") or (self.desired.get("source") or {}).get("name") or "coworker")

    def _agent_spec(self) -> Mapping[str, Any]:
        return (self.desired.get("agent") or {}).get("spec") or {}

    def effective_disabled(self, overlay: Mapping[str, Any] | None = None) -> list[str]:
        overlay = overlay or self.overlay()
        entry = (overlay.get("agents") or {}).get(self.agent)
        if isinstance(entry, Mapping) and isinstance(entry.get("disabledSkills"), list):
            return sorted(entry["disabledSkills"])
        return sorted(self._agent_spec().get("disabledSkills") or [])

    def skill_names(self) -> list[str]:
        names = list(self._agent_spec().get("skills") or [])
        for entry in (self._manifest or {}).get("skills", []):
            if isinstance(entry, Mapping) and entry.get("kind", "Skill") == "Skill" and entry.get("name") not in names:
                names.append(entry["name"])
        return sorted(set(names))

    def skill_rows(self, overlay: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Each Skill with whether it is switched on now and whether readiness would let it work (unknown readiness is not ready)."""
        disabled = set(self.effective_disabled(overlay))
        known = {e["name"]: e for e in (self._manifest or {}).get("skills", []) if isinstance(e, Mapping) and "name" in e}
        rows = []
        for name in self.skill_names():
            entry = known.get(name)
            rows.append({
                "name": name, "enabled": name not in disabled, "ready": bool(entry and entry.get("ready")), "known": entry is not None,
                "next": (entry or {}).get("remediation") or (None if entry else "No readiness report is available for this Skill."),
                "dependencies": [{k: d.get(k) for k in ("capability", "family", "status", "required", "provider", "tier")}
                                 for d in (entry or {}).get("dependencies", []) if isinstance(d, Mapping)],
            })
        return rows

    def _schedule_entries(self) -> list[dict[str, Any]]:
        try:
            entries = self._schedules() if self._schedules else []
        except Exception:  # noqa: BLE001 - an unreadable store shows no schedules and blocks edits (fail closed)
            return []
        overlay = self.overlay().get("schedules") or {}
        result = []
        for item in entries:
            merged = {k: item.get(k) for k in ("id", "agent", "owner", "cron", "event", "budget", "state", "goalDigest", "nextFire") if k in item}
            if item.get("id") in overlay:
                merged.update(overlay[item["id"]])
                merged["pendingWorkspaceChange"] = True
            result.append(merged)
        return result

    def effective_rules(self, overlay: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        policy = _mod("policy")
        rules = policy.rules_of(self.desired.get("policies"))
        for rule_id, replacement in ((overlay or self.overlay()).get("policyRules") or {}).items():
            index = next((i for i, r in enumerate(rules) if r.get("id") == rule_id), None)
            if replacement is None:
                if index is not None:
                    rules.pop(index)
            elif index is None:
                rules.append(dict(replacement))
            else:
                rules[index] = dict(replacement)
        return rules

    def coworkers(self) -> list[dict[str, Any]]:
        """The deployed coworker with its Skills, Tools, grants, blockers and schedules, plus coworkers created here but not yet deployed."""
        overlay = self.overlay()
        rows = self.skill_rows(overlay)
        manifest = self._manifest or {}
        grants = [{k: e.get(k) for k in ("family", "provider", "tier", "status", "grant", "tools")} for e in manifest.get("capabilities", [])
                  if isinstance(e, Mapping) and e.get("provider")]
        blockers = [{"skill": r["name"], "next": r["next"]} for r in rows if not r["ready"] and r["next"]]
        result = [{
            "name": self.agent, "deployed": True, "skills": rows, "tools": sorted(self._agent_spec().get("tools") or []), "grants": grants,
            "blockers": blockers, "schedules": self._schedule_entries(), "policyRules": [r.get("id") for r in self.effective_rules(overlay)],
        }]
        for name, entry in sorted((overlay.get("coworkers") or {}).items()):
            spec = (entry.get("resource") or {}).get("spec") or {}
            result.append({
                "name": name, "deployed": False, "template": entry.get("template"),
                "skills": [{"name": s, "enabled": s not in (spec.get("disabledSkills") or []), "ready": False, "known": True,
                            "next": "Becomes active after the workspace change is merged and the coworker has its own access."} for s in spec.get("skills") or []],
                "tools": sorted(spec.get("tools") or []), "grants": [], "blockers": [], "schedules": [], "policyRules": [],
            })
        return result

    # -- evidence -----------------------------------------------------------------------------------------

    def _knowledge_check(self, overlay: Mapping[str, Any]) -> dict[str, Any]:
        evidence = self._read_json(self.root / "knowledge-evidence.json")
        promotion = _mod("promotion")
        try:
            return promotion.check(self.desired, overlay.get("knowledgePromotion"), evidence, now=int(self.clock()),
                                   promoted_by=self.owner, owners=[self.owner])
        except promotion.PromotionError as exc:
            return {"allowed": False, "critical": [], "reasons": [f"record-unreadable:{type(exc).__name__}"], "summary": {}}

    def _knowledge_item(self, overlay: Mapping[str, Any], *, label: str = "Evals re-run against the exact business knowledge") -> dict[str, Any]:
        result = self._knowledge_check(overlay)
        if result["allowed"]:
            detail = ("evals cover " + ", ".join(result["critical"])) if result["critical"] else "no critical knowledge change is pending"
        else:
            detail = "; ".join(result["reasons"])
        return _item("knowledge-evals", label, result["allowed"], detail)

    def _needs_knowledge(self, skill: str) -> bool:
        entry = next((e for e in (self._manifest or {}).get("skills", []) if isinstance(e, Mapping) and e.get("name") == skill), None)
        return bool(entry and any(d.get("family") == "kb" for d in entry.get("dependencies", []) if isinstance(d, Mapping)))

    def _skill_items(self, skill: str, enable: bool, overlay: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not enable:
            return [_item(f"reduces:{skill}", f"Switching off {skill} only reduces what the coworker can do", True)]
        row = next((r for r in self.skill_rows(overlay) if r["name"] == skill), None)
        items = [_item(f"readiness:{skill}", f"{skill} is ready (its connections and this coworker's own access are in place)",
                       bool(row and row["ready"]), "ready" if row and row["ready"] else (row or {}).get("next") or "no readiness report")]
        if self._needs_knowledge(skill):
            items.append(self._knowledge_item(overlay))
        return items

    def _members(self, name: str, version: str) -> list[str]:
        skillsets = _mod("skillsets")
        available = {}
        for item in [*self.desired.get("skills", []), *self.desired.get("disabledSkillDeclarations", [])]:
            meta = item.get("metadata", {}) if isinstance(item, Mapping) else {}
            if meta.get("name"):
                available[meta["name"]] = str(meta.get("version", ""))
        try:
            return [row["skill"] for row in skillsets.resolve({"skillsets": [{"name": name, "version": version}]}, self.catalog, available)]
        except skillsets.SkillsetError as exc:
            raise ChangeError(f"that Skillset cannot be used here: {exc}"[:300]) from None

    # -- proposals ----------------------------------------------------------------------------------------

    def _draft(self, kind: str, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        """The deterministic content of a proposal: params, diff, summary (and the pack for a new coworker). Raises ChangeError."""
        if kind not in KINDS:
            raise ChangeError("unknown kind of change")
        if not isinstance(params, Mapping):
            raise ChangeError("the change needs its details")
        return getattr(self, "_draft_" + kind.replace("-", "_"))(params, overlay)

    # -- people and roles ---------------------------------------------------------------------------------

    def effective_access(self, overlay: Mapping[str, Any] | None = None) -> dict[str, list[str]]:
        """Each colleague's roles: the configured ones, replaced for anyone the owner has since changed here (the owner is implicit)."""
        roles = {person: list(held) for person, held in self._people.items()}
        for person, held in ((overlay or self.overlay()).get("access") or {}).items():
            roles[person] = list(held)
        return {person: held for person, held in sorted(roles.items()) if held and person != self.owner.lower()}

    def roles_of(self, person: Any, overlay: Mapping[str, Any] | None = None) -> list[str]:
        """The roles ``person`` holds; the named owner holds every role."""
        if not isinstance(person, str):
            return []
        key = person.strip().lower()
        if key == self.owner.lower():
            return sorted(ACCESS_ROLES)
        return self.effective_access(overlay).get(key, [])

    def _draft_access(self, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        person = _text(params.get("person"), "person", pattern=_PERSON).lower()
        action = params.get("action")
        if action not in ("grant", "revoke"):
            raise ChangeError("action must be grant or revoke")
        roles = params.get("roles")
        if not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles) or set(roles) - set(ACCESS_ROLES):
            raise ChangeError("roles must be a list of: " + ", ".join(sorted(ACCESS_ROLES)))
        if person == self.owner.lower():
            raise ChangeError("the owner's access is not changed here")
        wanted = sorted(set(roles))
        before = self.effective_access(overlay).get(person, [])
        after = sorted(set(before) | set(wanted)) if action == "grant" else sorted(set(before) - set(wanted))
        if after == before:
            raise ChangeError("that does not change anything")
        verb = "Give" if action == "grant" else "Take from"
        return {
            "params": {"person": person, "action": action, "roles": wanted},
            "diff": [{"field": f"access.{person}", "before": before, "after": after}],
            "summary": f"{verb} {person} the role{'s' if len(wanted) > 1 else ''} {', '.join(wanted)}",
        }

    def _access_items(self, change: Mapping[str, Any], overlay: Mapping[str, Any]) -> list[dict[str, Any]]:
        params, (row,) = change["params"], change["diff"]
        person = params["person"]
        items = [_item("owner-unchanged", "The owner's own access is not what is being changed", person != self.owner.lower()),
                 _item("access-unchanged", "The person still holds the roles this change was written against",
                       self.effective_access(overlay).get(person, []) == row["before"])]
        if params["action"] == "grant":
            if self._signin is None:
                items.append(_item("can-sign-in", "The person can sign in to the Studio", True,
                                   "The sign-in list is not available on the host; the gateway checks it when it applies the change.", required=False))
            else:
                items.append(_item("can-sign-in", "The person is on the web sign-in list", person in {a.strip().lower() for a in self._signin()},
                                   "Add their address to the web sign-in list first; a role is never given to someone who cannot sign in."))
        return items

    def _draft_skill(self, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        skill = _text(params.get("skill"), "skill", pattern=_NAME)
        enable = params.get("enabled")
        if not isinstance(enable, bool):
            raise ChangeError("say whether to switch the Skill on or off")
        if skill not in self.skill_names():
            raise ChangeError(f"{self.agent} has no Skill {skill}")
        before = self.effective_disabled(overlay)
        after = sorted(set(before) - {skill}) if enable else sorted(set(before) | {skill})
        if after == before:
            raise ChangeError(f"{skill} is already switched {'on' if enable else 'off'}")
        return {"params": {"skill": skill, "enabled": enable}, "diff": [{"field": f"agents/{self.agent}/disabledSkills", "before": before, "after": after}],
                "summary": f"Switch {skill} {'on' if enable else 'off'} for {self.agent}"}

    def _draft_skillset(self, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        name, version = _text(params.get("skillset"), "skillset", pattern=_NAME), _text(params.get("version"), "version")
        enable = params.get("enabled")
        if not isinstance(enable, bool):
            raise ChangeError("say whether to switch the Skillset's Skills on or off")
        members = self._members(name, version)
        before = self.effective_disabled(overlay)
        after = sorted(set(before) - set(members)) if enable else sorted(set(before) | set(members))
        if after == before:
            raise ChangeError(f"the Skills of {name}@{version} are already {'on' if enable else 'off'}")
        return {"params": {"skillset": name, "version": version, "enabled": enable, "members": members},
                "diff": [{"field": f"agents/{self.agent}/disabledSkills", "before": before, "after": after}],
                "summary": f"Switch the Skills of {name}@{version} {'on' if enable else 'off'} for {self.agent} (a Skillset grants no access; each member needs its own readiness)"}

    def _draft_schedule(self, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        schedules = _mod("schedules")
        schedule_id = _text(params.get("schedule"), "schedule")
        current = next((e for e in self._schedule_entries() if e.get("id") == schedule_id), None)
        if current is None:
            raise ChangeError("no such schedule")
        if not current.get("cron"):
            raise ChangeError("only a timed schedule can be edited here")
        diff: list[dict[str, Any]] = []
        out: dict[str, Any] = {"schedule": schedule_id}
        if params.get("cron") is not None:
            cron = " ".join(_text(params["cron"], "cron").split())
            try:
                schedules.parse_cron(cron)
                schedules._check_interval(cron)  # noqa: SLF001 - the same interval floor the schedule store applies
                if schedules.next_fire(cron, int(self.clock())) is None:
                    raise schedules.ScheduleError("this cron expression never fires")
            except schedules.ScheduleError as exc:
                raise ChangeError(str(exc)[:200]) from None
            if cron != current["cron"]:
                diff.append({"field": f"schedules/{schedule_id}/cron", "before": current["cron"], "after": cron})
            out["cron"] = cron
        if params.get("budget") is not None:
            try:
                budget = schedules._budget(params["budget"])  # noqa: SLF001
            except schedules.ScheduleError as exc:
                raise ChangeError(str(exc)[:200]) from None
            for key in sorted(set(budget) | set(current.get("budget") or {})):
                old, new = (current.get("budget") or {}).get(key), budget.get(key)
                if old != new:
                    diff.append({"field": f"schedules/{schedule_id}/budget/{key}", "before": old, "after": new})
            out["budget"] = budget
        if not diff:
            raise ChangeError("that schedule already has these values")
        return {"params": out, "diff": diff, "summary": f"Edit schedule {schedule_id} for {self.agent}"}

    def _rule_checked(self, rule: Any) -> dict[str, Any]:
        if not isinstance(rule, Mapping) or len(canonical(rule)) > MAX_RULE_BYTES:
            raise ChangeError("a Policy rule is a small object")
        policy = _mod("policy")
        rule = {k: v for k, v in rule.items() if k != "signoff"}
        if not isinstance(rule.get("id"), str) or not _NAME.match(rule["id"]) or rule.get("effect") not in policy.EFFECTS:
            raise ChangeError("a Policy rule needs an id and an effect (allow, deny, require-approval or human-only)")
        return json.loads(canonical(rule))

    def _draft_policy(self, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        policy = _mod("policy")
        action = params.get("action")
        before_rules = self.effective_rules(overlay)
        if action == "set":
            rule = self._rule_checked(params.get("rule"))
            after_rules = [rule if r.get("id") == rule["id"] else r for r in before_rules]
            if not any(r.get("id") == rule["id"] for r in before_rules):
                after_rules.append(rule)
            out: dict[str, Any] = {"action": "set", "rule": rule}
            rule_id = rule["id"]
        elif action == "remove":
            rule_id = _text(params.get("ruleId"), "ruleId", pattern=_NAME)
            if not any(r.get("id") == rule_id for r in before_rules):
                raise ChangeError("no such Policy rule")
            after_rules = [r for r in before_rules if r.get("id") != rule_id]
            out = {"action": "remove", "ruleId": rule_id}
        else:
            raise ChangeError("a Policy change is set or remove")
        if canonical(before_rules) == canonical(after_rules):
            raise ChangeError("the Policy already says this")
        summary = policy.diff_policies([{"spec": {"rules": before_rules}}], [{"spec": {"rules": after_rules}}])
        old = next((r for r in before_rules if r.get("id") == rule_id), None)
        new = next((r for r in after_rules if r.get("id") == rule_id), None)
        out["relaxes"] = rule_id in summary["relaxed"]
        out["policyVersions"] = [summary["previousVersion"], summary["newVersion"]]
        return {"params": out, "diff": [{"field": f"policy/rules/{rule_id}", "before": old, "after": new}],
                "summary": f"{'Relax' if out['relaxes'] else 'Change'} the approval Policy rule {rule_id}" + (" (needs the owner's signed sign-off)" if out["relaxes"] else "")}

    def signoff_subject(self, change: Mapping[str, Any]) -> dict[str, Any] | None:
        """The exact document the owner signs for a relaxing Policy change (the rule itself for a set; a removal statement otherwise)."""
        params = change["params"]
        if change["kind"] != "policy" or not params.get("relaxes"):
            return None
        if params["action"] == "set":
            return dict(params["rule"])
        removed = next((r for r in self.effective_rules() if r.get("id") == params["ruleId"]), None) or {}
        return {"id": params["ruleId"], "removal": True, "removedRuleDigest": _mod("policy").rule_digest(removed)}

    def _signoff_item(self, change: Mapping[str, Any], signoff: Any) -> dict[str, Any]:
        label = "The owner signed this exact change (Ed25519)"
        subject = self.signoff_subject(change)
        policy = _mod("policy")
        key = self.owner_keys.get(self.owner.strip().lower())
        if key is None:
            return _item("owner-signoff", label, False, "no owner key is configured (studio.ownerKeys), so a relaxing change cannot be approved")
        if not isinstance(signoff, Mapping):
            return _item("owner-signoff", label, False, f"sign this document with the owner key and attach it; digest {policy.rule_digest(subject)}")
        base = {k: v for k, v in signoff.items() if k != "signoff"}
        if canonical(base) != canonical(subject):
            return _item("owner-signoff", label, False, "the signed document is not this change's exact content")
        owner_named = isinstance(signoff.get("signoff"), Mapping) and str(signoff["signoff"].get("owner", "")).strip().lower() == self.owner.lower()
        valid = owner_named and policy.signature_valid(dict(signoff), key)
        return _item("owner-signoff", label, valid, "valid signature by the named owner" if valid else "the signature is missing, not by the named owner, or cryptography is not installed")

    def _policy_items(self, change: Mapping[str, Any], overlay: Mapping[str, Any], signoff: Any) -> list[dict[str, Any]]:
        policy = _mod("policy")
        params = change["params"]
        target = (params.get("rule") or {}).get("id", params.get("ruleId"))
        rules = [r for r in self.effective_rules(overlay) if r.get("id") != target]
        if params["action"] == "set":
            rule = dict(params["rule"])
            if params.get("relaxes") and isinstance(signoff, Mapping) and isinstance(signoff.get("signoff"), Mapping):
                rule["signoff"] = signoff["signoff"]
            rules.append(rule)
        errors = [e for e in policy.relaxation_errors([{"spec": {"rules": rules}}], self.desired.get("tools", []))
                  if "sign-off" not in e and "signoff" not in e]
        items = [_item("policy-valid", "The resulting Policy passes the compiler's rules", not errors, "; ".join(errors)[:300])]
        if params.get("relaxes"):
            items.append(self._signoff_item(change, signoff))
        else:
            items.append(_item("no-relaxation", "This change does not loosen any approval requirement", True))
        return items

    def _draft_knowledge(self, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        if params.get("action") != "promote":
            raise ChangeError("the only knowledge change is promote")
        current = _mod("promotion")._deploy().fingerprint(self.desired)  # noqa: SLF001
        promoted = ((overlay.get("knowledgePromotion") or {}).get("promoted") or {}).get("sources") or {}

        def short(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
            return None if entry is None else {k: (entry[k][:12] if k == "sha256" else entry[k]) for k in ("status", "version", "sha256") if k in entry}

        diff = [{"field": f"knowledge/{name}", "before": short(promoted.get(name)), "after": short(current.get(name))}
                for name in sorted(set(current) | set(promoted)) if promoted.get(name) != current.get(name)]
        if not diff:
            raise ChangeError("the business knowledge already matches the promoted record")
        return {"params": {"action": "promote"}, "diff": diff, "summary": f"Promote changed business knowledge ({len(diff)} source(s)) for {self.agent}"}

    def _grantless(self, name: str) -> dict[str, Any]:
        """The manifest as the new coworker would see it: no grant of its own yet, so anything connected is not ready."""
        return _mod("readiness").overlay({**(self._manifest or {"skills": [], "capabilities": []}), "agent": name}, [], now=int(self.clock()))

    def _draft_new_coworker(self, params: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        name = _text(params.get("name"), "name", pattern=_NAME)
        template = _text(params.get("template", self.agent), "template", pattern=_NAME)
        title = str(params.get("roleTitle") or "").strip()[:80]
        if template != self.agent:
            raise ChangeError(f"the only template available here is {self.agent}")
        if name == self.agent or name in (overlay.get("coworkers") or {}):
            raise ChangeError("a coworker with that name already exists")
        resource = copy.deepcopy(self.desired.get("agent") or {})
        resource.setdefault("metadata", {})["name"] = name
        if title:
            resource["metadata"]["description"] = f"{title} (created from the {template} template)"
        manifest = self._grantless(name)
        not_ready = {e["name"] for e in manifest.get("skills", []) if e.get("kind", "Skill") == "Skill" and not e.get("ready")}
        spec = resource.setdefault("spec", {})
        skills = list(spec.get("skills") or [])
        spec["disabledSkills"] = sorted((set(spec.get("disabledSkills") or []) | not_ready) & set(skills))
        entries = [{**e, "enabled": e["name"] not in spec["disabledSkills"]} for e in manifest.get("skills", [])]
        pack = self._pack(name, entries)
        return {"params": {"name": name, "template": template, "roleTitle": title, "resource": resource, "packDigest": sha(pack)},
                "pack": pack,
                "diff": [{"field": f"coworkers/{name}", "before": None,
                          "after": {"template": template, "skills": skills, "disabledSkills": spec["disabledSkills"], "tools": spec.get("tools", [])}}],
                "summary": f"Create coworker {name} from the {template} template (Skills that need their own access start switched off)"}

    def _pack(self, name: str, skill_entries: list[dict[str, Any]]) -> str:
        pack, policy = _mod("pack"), _mod("policy")
        rules = self.effective_rules()
        actions = []
        for tool in self.desired.get("tools", []):
            spec = tool.get("spec", {}) if isinstance(tool, Mapping) else {}
            tool_name = tool.get("metadata", {}).get("name", "")
            outcome = policy.preview([{"spec": {"rules": rules}}], tool_name=tool_name, risk=spec.get("risk"),
                                     tool_requires_approval=bool(spec.get("approvalRequired")))
            actions.append({"tool": tool_name, "risk": spec.get("risk"), "member": outcome[policy.MEMBER], "public": outcome[policy.PUBLIC]})
        readiness = [{"agent": name, "environment": (self._manifest or {}).get("environment"), "skills": skill_entries}]
        approvals = [{"agent": name, "policyVersion": policy.policy_version([{"spec": {"rules": rules}}]), "skills": [{"skill": None, "actions": actions}]}]
        try:
            return pack.render(pack.build(readiness, approvals))
        except pack.PackError as exc:
            raise ChangeError(f"the onboarding pack could not be written: {exc}"[:200]) from None

    def _new_coworker_items(self, change: Mapping[str, Any], overlay: Mapping[str, Any]) -> list[dict[str, Any]]:
        params = change["params"]
        free = params["name"] not in (overlay.get("coworkers") or {}) and params["name"] != self.agent
        spec = params["resource"].get("spec", {})
        started = {s for s in spec.get("skills", []) if s not in spec.get("disabledSkills", [])}
        unsafe = sorted(e["name"] for e in self._grantless(params["name"]).get("skills", []) if e["name"] in started and not e.get("ready"))
        return [
            _item("template-exists", f"The template {params['template']} is the deployed coworker", params["template"] == self.agent),
            _item("name-free", f"The name {params['name']} is not in use", free),
            _item("starts-safe", "Every Skill that needs the new coworker's own access starts switched off", not unsafe, ", ".join(unsafe)),
            _item("pack-matches", "The onboarding pack the owner reviewed is the one that will be kept", sha(change.get("pack")) == params["packDigest"]),
        ]

    def _schedule_items(self, change: Mapping[str, Any]) -> list[dict[str, Any]]:
        label = "The cron expression fires and the budget is well formed"
        try:
            again = self._draft_schedule(change["params"], self.overlay())
        except ChangeError as exc:
            return [_item("schedule-valid", label, False, str(exc))]
        return [_item("schedule-valid", label, True, "valid"),
                _item("schedule-unchanged-base", "The schedule still has the values this change started from", again["diff"] == change["diff"])]

    def evidence(self, change: Mapping[str, Any], attachments: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """The evidence checklist, recomputed now. Every required item must be met to approve."""
        attachments = attachments or {}
        overlay = self.overlay()
        kind, params = change["kind"], change["params"]
        try:
            if kind == "skill":
                return self._skill_items(params["skill"], params["enabled"], overlay)
            if kind == "skillset":
                unchanged = self._members(params["skillset"], params["version"]) == params["members"]
                items = [_item("skillset-unchanged", "The Skillset still contains the Skills this change listed", unchanged)]
                disabled = set(self.effective_disabled(overlay))
                knowledge_seen = False
                for skill in params["members"] if unchanged else []:
                    if (skill in disabled) != params["enabled"]:
                        continue
                    for item in self._skill_items(skill, params["enabled"], overlay):
                        if item["id"] == "knowledge-evals":
                            if knowledge_seen:
                                continue
                            knowledge_seen = True
                        items.append(item)
                return items
            if kind == "schedule":
                return self._schedule_items(change)
            if kind == "policy":
                return self._policy_items(change, overlay, attachments.get("signoff"))
            if kind == "knowledge":
                return [self._knowledge_item(overlay, label="Evals ran against the exact content being promoted, by a named owner")]
            if kind == "access":
                return self._access_items(change, overlay)
            return self._new_coworker_items(change, overlay)
        except ChangeError as exc:
            return [_item("proposal-valid", "The proposal can still be made", False, str(exc))]

    # -- lifecycle ----------------------------------------------------------------------------------------

    def _require_owner(self, by: Any) -> str:
        try:
            who = _text(by, "who", pattern=_PERSON)
        except ChangeError:
            raise ChangeRefused(["not-the-owner"]) from None
        if who.lower() != self.owner.lower():
            raise ChangeRefused(["not-the-owner"])
        return who

    def _require_author(self, by: Any, kind: str) -> str:
        """The owner, or a colleague holding ``author`` for any kind but ``access`` (who may hold which role is the owner's alone)."""
        try:
            return self._require_owner(by)
        except ChangeRefused:
            if kind != "access" and isinstance(by, str) and "author" in self.roles_of(by):
                return by.strip()
            raise

    def propose(self, kind: str, params: Mapping[str, Any], *, by: str) -> dict[str, Any]:
        who = self._require_author(by, kind)
        now = int(self.clock())
        with self._locked():
            overlay = self.overlay()
            if sum(1 for d in self.list() if d.get("state") == "proposed") >= MAX_OPEN:
                raise ChangeRefused(["too-many-open-changes"])
            draft = self._draft(kind, params, overlay)
            change: dict[str, Any] = {
                "schemaVersion": SCHEMA_VERSION, "id": "cr-" + secrets.token_hex(6), "kind": kind, "agent": self.agent, "owner": self.owner,
                "proposedBy": who, "proposedAt": _iso(now), "proposedEpoch": now, "state": "proposed", "baseVersion": overlay["version"],
                "params": draft["params"], "diff": draft["diff"], "summary": draft["summary"],
                **({"pack": draft["pack"]} if "pack" in draft else {}),
            }
            change["digest"] = sha({k: change[k] for k in ("kind", "agent", "owner", "params", "diff", "baseVersion")})
            change["evidence"] = self.evidence(change)
            subject = self.signoff_subject(change)
            if subject is not None:
                change["signoffSubject"] = subject
                change["signoffDigest"] = _mod("policy").rule_digest(subject)
            change["history"] = [{"at": _iso(now), "by": who, "action": "proposed"}]
            self._record("studio.change-proposed", who, {"id": change["id"], "kind": kind, "agent": self.agent, "digest": change["digest"]})
            self._write(self._path(change["id"]), change)
        return change

    def _apply_to_overlay(self, change: Mapping[str, Any], overlay: dict[str, Any], now: int) -> dict[str, Any]:
        new = copy.deepcopy(overlay)
        kind, params = change["kind"], change["params"]
        if kind in ("skill", "skillset"):
            new["agents"].setdefault(self.agent, {})["disabledSkills"] = change["diff"][0]["after"]
        elif kind == "schedule":
            new["schedules"][params["schedule"]] = {k: params[k] for k in ("cron", "budget") if k in params}
        elif kind == "policy":
            signed = change.get("approval", {}).get("signoff")
            rule_id = params.get("rule", {}).get("id", params.get("ruleId"))
            if params["action"] == "remove":
                new["policyRules"][rule_id] = None
            else:
                new["policyRules"][rule_id] = {**params["rule"], "signoff": signed["signoff"]} if signed and params.get("relaxes") else params["rule"]
        elif kind == "knowledge":
            new["knowledgePromotion"] = _mod("promotion").promote(
                self.desired, overlay.get("knowledgePromotion"), self._read_json(self.root / "knowledge-evidence.json"),
                by=self.owner, now=now, owners=[self.owner])
        elif kind == "access":
            new["access"][params["person"]] = change["diff"][0]["after"]
        else:
            new["coworkers"][params["name"]] = {"resource": params["resource"], "template": params["template"], "packDigest": params["packDigest"], "createdAt": _iso(now)}
        new["version"] = overlay["version"] + 1
        new["lastChange"] = change["id"]
        return new

    def approve(self, change_id: str, *, by: str, digest: str, attachments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Apply the change. The caller must be the named owner, name the exact digest they reviewed, and every required evidence item must be met."""
        who = self._require_owner(by)
        now = int(self.clock())
        with self._locked():
            change = self.get(change_id)
            overlay = self.overlay()
            if change.get("state") == "applying":
                return self._finish(change, overlay, who)
            if change.get("state") != "proposed":
                raise ChangeRefused([f"already-{change.get('state')}"])
            reasons: list[str] = []
            if digest != change["digest"]:
                reasons.append("digest-mismatch")
            if now - int(change.get("proposedEpoch", 0)) > PROPOSAL_TTL:
                reasons.append("proposal-expired")
            if change["baseVersion"] != overlay["version"]:
                reasons.append("stale-base")
            if change["agent"] != self.agent or change["owner"] != self.owner:
                reasons.append("coworker-or-owner-changed")
            if not reasons:
                try:
                    redone = self._draft(change["kind"], change["params"], overlay)
                    if redone["diff"] != change["diff"] or redone["params"] != change["params"]:
                        reasons.append("changed-since-proposed")
                except ChangeError:
                    reasons.append("changed-since-proposed")
            items = self.evidence(change, attachments) if not reasons else change["evidence"]
            reasons += [f"evidence:{item['id']}" for item in items if item["required"] and not item["met"]]
            if reasons:
                self._record("studio.change-blocked", who, {"id": change_id, "kind": change["kind"], "reasons": reasons[:20]})
                raise ChangeRefused(reasons)
            signoff = (attachments or {}).get("signoff") if change["kind"] == "policy" else None
            approval = {"by": who, "at": _iso(now), "digest": change["digest"], "evidenceDigest": sha(items), **({"signoff": signoff} if signoff else {})}
            applied = self._apply_to_overlay({**change, "approval": approval}, overlay, now)
            self._record("studio.change-approved", who, {"id": change_id, "kind": change["kind"], "agent": self.agent, "digest": change["digest"],
                                                         "evidenceDigest": approval["evidenceDigest"], "overlayVersion": applied["version"]})
            change.update(state="applying", evidence=items, approval=approval,
                          rollback={"beforeOverlay": overlay, "afterOverlay": applied, "recordedAt": _iso(now)})
            change["history"].append({"at": _iso(now), "by": who, "action": "approved"})
            self._write(self._path(change_id), change)
            return self._finish(change, overlay, who)

    def _finish(self, change: dict[str, Any], current: Mapping[str, Any], who: str) -> dict[str, Any]:
        """Write the overlay (idempotently) and mark the change applied."""
        after = change["rollback"]["afterOverlay"]
        if canonical(current) != canonical(after):
            if current.get("version") != change["rollback"]["beforeOverlay"]["version"]:
                raise ChangeRefused(["stale-base"])
            self._write(self.root / "overlay.json", after)
        change["state"] = "applied"
        change["appliedAt"] = _iso(int(self.clock()))
        change["history"].append({"at": change["appliedAt"], "by": who, "action": "applied"})
        self._write(self._path(change["id"]), change)
        self._record("studio.change-applied", who, {"id": change["id"], "kind": change["kind"], "agent": self.agent, "overlayVersion": after["version"]})
        return change

    def reject(self, change_id: str, *, by: str, reason: str = "") -> dict[str, Any]:
        who = self._require_owner(by)
        with self._locked():
            change = self.get(change_id)
            if change.get("state") != "proposed":
                raise ChangeRefused([f"already-{change.get('state')}"])
            change["state"] = "rejected"
            change["history"].append({"at": _iso(int(self.clock())), "by": who, "action": "rejected", "reason": str(reason)[:MAX_TEXT]})
            self._record("studio.change-rejected", who, {"id": change_id, "kind": change["kind"]})
            self._write(self._path(change_id), change)
            return change

    def rollback(self, change_id: str, *, by: str) -> dict[str, Any]:
        """Restore the overlay as it was before this change, if it is the latest one applied."""
        who = self._require_owner(by)
        now = int(self.clock())
        with self._locked():
            change = self.get(change_id)
            overlay = self.overlay()
            if change.get("state") != "applied":
                raise ChangeRefused([f"not-applied:{change.get('state')}"])
            if overlay.get("lastChange") != change_id:
                raise ChangeRefused(["later-change-applied"])
            restored = copy.deepcopy(change["rollback"]["beforeOverlay"])
            restored["version"] = overlay["version"] + 1
            self._record("studio.change-rolled-back", who, {"id": change_id, "kind": change["kind"], "agent": self.agent, "overlayVersion": restored["version"]})
            self._write(self.root / "overlay.json", restored)
            change["state"] = "rolled-back"
            change["rolledBack"] = {"by": who, "at": _iso(now)}
            change["history"].append({"at": _iso(now), "by": who, "action": "rolled-back"})
            self._write(self._path(change_id), change)
            return change

    def export_patch(self, change_id: str) -> dict[str, Any]:
        """The workspace change the operator commits for an applied change (names, values and signed rules only; no secrets)."""
        change = self.get(change_id)
        if change.get("state") != "applied":
            raise ChangeError("only an applied change has a workspace patch")
        kind, params = change["kind"], change["params"]
        after = change["rollback"]["afterOverlay"]
        patch: dict[str, Any] = {"change": change_id, "kind": kind, "agent": change["agent"], "approvedBy": change["approval"]["by"], "digest": change["digest"]}
        if kind in ("skill", "skillset"):
            patch["agentSpec"] = {"disabledSkills": change["diff"][0]["after"]}
        elif kind == "schedule":
            patch["schedule"] = {"id": params["schedule"], **{k: params[k] for k in ("cron", "budget") if k in params}}
        elif kind == "policy":
            patch["policyRule"] = after["policyRules"].get(params.get("rule", {}).get("id", params.get("ruleId")))
            patch["removeRule"] = params.get("ruleId")
        elif kind == "knowledge":
            patch["promotionRecord"] = after["knowledgePromotion"]
        elif kind == "access":
            roles = change["diff"][0]["after"]
            patch["access"] = {"person": params["person"], "roles": roles, "studioPeople": {params["person"]: roles},
                               "approverInGatewayDirectory": "approver" in roles}
        else:
            patch.update(agentResource=params["resource"], onboardingPack=change["pack"])
        return patch


def load_store(
    data_root: pathlib.Path, desired_state: Mapping[str, Any], *, tenant: str, studio: Mapping[str, Any], manifest_path: str | None = None,
    audit: Audit | None = None, clock: Callable[[], float] = time.time, schedules: Callable[[], list[dict[str, Any]]] | None = None,
    signin: Callable[[], Iterable[str]] | None = None,
) -> ChangeStore:
    """Build the store the gateway and the operator command share: manifest with the coworker's own grants applied, owner and keys from config."""
    manifest = None
    if manifest_path:
        try:
            manifest = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
    if isinstance(manifest, dict):
        manifest = _mod("readiness").overlay_from_root(manifest, pathlib.Path(data_root) / "provider-grants", now=int(clock()))
    return ChangeStore(data_root, tenant=tenant, desired_state=desired_state, manifest=manifest, owner=studio["owner"],
                       owner_keys=studio.get("ownerKeys"), audit=audit, clock=clock, schedules=schedules,
                       people=studio.get("people"), signin=signin)
