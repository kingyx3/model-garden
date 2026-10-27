#!/usr/bin/env python3
"""Authorize and execute Model Garden Tool actions through a minimal governed boundary.

MVP behavior is intentionally small and explicit:
- an Agent may request only Tools present in its compiled desired state;
- selected Tools resolve to allow, require-approval or deny from the stricter of Agent, Tool and
  the client's Policy rules (platform/core/policy.py), with platform safety floors for public
  principals and privileged Tools;
- approvals bind to an immutable requestId derived from agent + tool + exact arguments + initiating identity/context;
- denied, pending, approved, failed, and executed material actions can be written as versioned JSONL audit events;
- schema-v3 events store arguments redacted plus a digest, a bounded ledger block (delegation chain,
  stated justification, policy rule, capability/provider/tier, external object reference), and
  read events that record which sources and records were read as digests, never content;
- a verified channel principal can flow in as the initiating identity; unmapped (public) principals
  are limited to read-class Tools;
- first-party governance context can correlate tenant, environment, conversation, runtime, model and commercial attribution;
- execution is injected by the caller, so this module never owns provider credentials.

This is not a general policy engine: rules are ordered, glob-matched and fail-closed. Richer
policy semantics (bounded conditions, counters) belong behind the same boundary only after
repeated client requirements justify them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
from collections.abc import Callable
from typing import Any

AUDIT_SCHEMA_VERSION = 3
CHAINED_SCHEMA_VERSIONS = (2, 3)
POLICY_VERSION = "model-garden-governance/1"
REDACTED = "[redacted]"
MAX_CONTEXT_VALUE = 256
MAX_JUSTIFICATION = 500
MAX_DELEGATION_DEPTH = 16
READ_AUDIT_MODES = ("full", "aggregate", "sampled")
DEFAULT_MAX_RECORD_IDS = 100
GOVERNANCE_CONTEXT_KEYS = (
    "tenantId",
    "environment",
    "deploymentId",
    "conversationId",
    "sessionId",
    "runtime",
    "runtimeVersion",
    "modelProvider",
    "model",
    "modelConfigVersion",
    "credentialScope",
    "correlationId",
    "costCenter",
    "billingSubscriptionId",
    "sharedPoolId",
    "originatingPrincipal",
    "channel",
    "triggerRef",
    "taskId",
    "parentRequestId",
)
# Bounded authority fields (bind the requestId) that are not plain strings.
GOVERNANCE_LIST_KEYS = ("delegationChain",)
# Bounded descriptive fields recorded on ledger events but not bound into the requestId,
# so a reworded justification or a new platform release cannot invalidate an approval.
LEDGER_STRING_KEYS = (
    "agentVersion",
    "workspaceRevision",
    "platformRelease",
    "policyRuleId",
    "policyVersion",
    "justification",
    "capability",
    "provider",
    "externalObjectRef",
    "grantId",
    "connectionId",
    "actingIdentity",
    "upstreamTool",
    "principalAccess",
)
LEDGER_INT_KEYS = ("tier",)
_BOUNDED_KEYS = frozenset({"originatingPrincipal", "channel", "triggerRef", "taskId", "parentRequestId"})


def _load_capabilities():
    path = pathlib.Path(__file__).resolve().parents[1] / "platform" / "core" / "capabilities.py"
    module_spec = importlib.util.spec_from_file_location("model_garden_capabilities_for_governance", path)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


CAPABILITIES = _load_capabilities()


def _load_policy():
    path = pathlib.Path(__file__).resolve().parents[1] / "platform" / "core" / "policy.py"
    module_spec = importlib.util.spec_from_file_location("model_garden_policy_for_governance", path)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


POLICY = _load_policy()


def _load_handoff():
    path = pathlib.Path(__file__).resolve().parents[1] / "platform" / "core" / "handoff.py"
    module_spec = importlib.util.spec_from_file_location("model_garden_handoff_for_governance", path)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


HANDOFF = _load_handoff()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _agent_name(desired_state: dict[str, Any]) -> str:
    source = desired_state.get("source", {})
    name = source.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("Governed action failed: desired state is missing source.name")
    return name


def _selected_tool(desired_state: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    for tool in desired_state.get("tools", []):
        if tool.get("metadata", {}).get("name") == tool_name:
            return tool
    return None


def _governance_context(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Governed action failed: governance_context must be a JSON object")
    unknown = sorted(set(value) - set(GOVERNANCE_CONTEXT_KEYS) - set(GOVERNANCE_LIST_KEYS))
    if unknown:
        raise ValueError("Governed action failed: unsupported governance context keys: " + ", ".join(unknown))
    context: dict[str, Any] = {}
    for key in GOVERNANCE_CONTEXT_KEYS:
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or not item:
                raise ValueError(f"Governed action failed: governance context {key} must be a non-empty string")
            if key in _BOUNDED_KEYS and len(item) > MAX_CONTEXT_VALUE:
                raise ValueError(f"Governed action failed: governance context {key} exceeds {MAX_CONTEXT_VALUE} characters")
            context[key] = item
    chain = value.get("delegationChain")
    if chain is not None:
        if (
            not isinstance(chain, list)
            or not chain
            or len(chain) > MAX_DELEGATION_DEPTH
            or not all(isinstance(hop, str) and hop and len(hop) <= MAX_CONTEXT_VALUE for hop in chain)
        ):
            raise ValueError(
                f"Governed action failed: governance context delegationChain must be 1-{MAX_DELEGATION_DEPTH} non-empty strings"
            )
        context["delegationChain"] = list(chain)
    return context


def ledger_context(value: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the bounded, allow-listed ledger block; arbitrary fields are rejected."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Governed action failed: ledger context must be a JSON object")
    unknown = sorted(set(value) - set(LEDGER_STRING_KEYS) - set(LEDGER_INT_KEYS))
    if unknown:
        raise ValueError("Governed action failed: unsupported ledger context keys: " + ", ".join(unknown))
    context: dict[str, Any] = {}
    for key in LEDGER_STRING_KEYS:
        item = value.get(key)
        if item is None:
            continue
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Governed action failed: ledger context {key} must be a non-empty string")
        if key == "justification":
            item = " ".join(item.split())[:MAX_JUSTIFICATION]
        elif len(item) > MAX_CONTEXT_VALUE:
            raise ValueError(f"Governed action failed: ledger context {key} exceeds {MAX_CONTEXT_VALUE} characters")
        context[key] = item
    tier = value.get("tier")
    if tier is not None:
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in CAPABILITIES.TIERS:
            raise ValueError("Governed action failed: ledger context tier must be an integer 0-4")
        context["tier"] = tier
    return context


def redact_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep only catalog-approved scalar argument fields verbatim; redact everything else."""
    keep = CAPABILITIES.ledger_fields(tool_name)
    redacted: dict[str, Any] = {}
    for key in sorted(arguments):
        value = arguments[key]
        safe_scalar = isinstance(value, (bool, int, float)) or (isinstance(value, str) and len(value) <= MAX_CONTEXT_VALUE)
        redacted[key] = value if key in keep and safe_scalar else REDACTED
    return redacted


def _tool_risk(desired_state: dict[str, Any], tool_name: str) -> str | None:
    tool = _selected_tool(desired_state, tool_name)
    if tool is None:
        return None
    risk = tool.get("spec", {}).get("risk")
    return risk if isinstance(risk, str) else None


def build_action_request(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    initiating_user: str | None = None,
    governance_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("Governed action failed: arguments must be a JSON object")

    material: dict[str, Any] = {
        "agent": _agent_name(desired_state),
        "tool": tool_name,
        "arguments": arguments,
    }
    if initiating_user:
        material["initiatingUser"] = initiating_user
    context = _governance_context(governance_context)
    if context:
        material["governance"] = context
    request_id = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return {**material, "requestId": request_id}


def count_executions_today(
    audit_path: pathlib.Path | None,
    tool_name: str,
    *,
    now: dt.datetime | None = None,
    agent: str | None = None,
    initiating_user: str | None = None,
) -> int | None:
    """Count successful executions in a *verified*, stable ledger snapshot.

    A valid-looking JSON edit must not reset a signed policy's daily allowance.
    This is a host-local tamper-evidence check, not an independent truncation
    anchor or a cross-process atomic reservation of an execution slot.
    """
    if audit_path is None:
        return None
    today = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    if not audit_path.exists():
        return 0  # legacy first-run ledger; no independent initialization witness
    count = 0
    try:
        with audit_path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                opened = os.fstat(handle.fileno())
                current = audit_path.lstat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    return None  # swapped path or symlink
                _verify_audit_chain_handle(handle)
                handle.seek(0)
                for raw in handle:
                    event = json.loads(raw)
                    if (
                        isinstance(event, dict)
                        and event.get("event") == "execution"
                        and event.get("outcome") == "succeeded"
                        and event.get("tool") == tool_name
                        and str(event.get("timestamp", "")).startswith(today)
                        and (agent is None or event.get("agent") in (None, agent))
                        and (initiating_user is None or event.get("initiatingUser") == initiating_user)
                    ):
                        count += 1
                current = audit_path.lstat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    return None
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError, TypeError, UnicodeError):
        return None
    return count

def authorize_action(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    initiating_user: str | None = None,
    governance_context: dict[str, Any] | None = None,
    principal_access: str | None = None,
    audit_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    request = build_action_request(
        desired_state,
        tool_name,
        arguments,
        initiating_user=initiating_user,
        governance_context=governance_context,
    )
    tool = _selected_tool(desired_state, tool_name)
    if tool is None:
        return {"decision": "deny", "reason": "tool-not-selected", "request": request}
    if principal_access not in (None, "member", "public"):
        raise ValueError("Governed action failed: principal_access must be member or public")

    agent_spec = desired_state.get("agent", {}).get("spec", {})
    conditions = [rule["conditions"] for rule in POLICY.rules_of(desired_state.get("policies")) if isinstance(rule.get("conditions"), dict)]
    limited = any("maxPerDay" in item for item in conditions)
    limited_per_person = any("maxPerPrincipalPerDay" in item for item in conditions)
    agent_name = _agent_name(desired_state)
    outcome = POLICY.evaluate(
        desired_state.get("policies"),
        arguments=arguments,
        usage_today=count_executions_today(audit_path, tool_name, agent=agent_name) if limited else None,
        # Unattributed requesters have no count of their own, so a per-person limit is not proven and the rule does not apply.
        usage_by_principal=(
            count_executions_today(audit_path, tool_name, agent=agent_name, initiating_user=initiating_user)
            if limited_per_person and initiating_user else None
        ),
        tool_name=tool_name,
        risk=tool.get("spec", {}).get("risk"),
        classification=agent_spec.get("security", {}).get("dataClassification"),
        channel=request.get("governance", {}).get("channel"),
        principal=principal_access,
        agent_requires_approval=agent_spec.get("approvals", {}).get("externalActions") == "required",
        tool_requires_approval=bool(tool.get("spec", {}).get("approvalRequired", False)),
    )
    return {
        "decision": outcome["decision"],
        "reason": outcome["reason"],
        "ruleId": outcome["ruleId"],
        "assignee": outcome.get("assignee"),
        "deadlineHours": outcome.get("deadlineHours"),
        "request": request,
    }


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def make_audit_event(
    event_type: str,
    request: dict[str, Any],
    *,
    outcome: str,
    details: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one schema-v3 ledger event: redacted arguments plus digest, never raw payloads."""
    event = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "timestamp": _utc_timestamp(),
        "event": event_type,
        "outcome": outcome,
        "requestId": request["requestId"],
        "agent": request["agent"],
        "tool": request["tool"],
        "arguments": redact_arguments(request["tool"], request["arguments"]),
        "argumentsDigest": _sha256_json(request["arguments"]),
    }
    if "initiatingUser" in request:
        event["initiatingUser"] = request["initiatingUser"]
    if "governance" in request:
        event["governance"] = request["governance"]
    bounded = ledger_context(ledger)
    if bounded:
        event["ledger"] = bounded
    if details:
        event["details"] = details
    return event


def parse_read_policy(value: str | dict[str, Any] | None) -> dict[str, Any]:
    """Parse ``full`` (default), ``aggregate`` or ``sampled:<N>`` into a read-audit policy."""
    if value is None or value == "":
        return {"mode": "full", "maxRecordIds": DEFAULT_MAX_RECORD_IDS}
    if isinstance(value, dict):
        mode = value.get("mode", "full")
        rate = value.get("sampleRate")
        max_ids = value.get("maxRecordIds", DEFAULT_MAX_RECORD_IDS)
    else:
        mode, _, rate_text = value.strip().partition(":")
        rate = int(rate_text) if rate_text.isdigit() else None
        max_ids = DEFAULT_MAX_RECORD_IDS
    if mode not in READ_AUDIT_MODES:
        raise ValueError(f"read audit mode must be one of {', '.join(READ_AUDIT_MODES)}")
    if not isinstance(max_ids, int) or isinstance(max_ids, bool) or max_ids < 1:
        raise ValueError("read audit maxRecordIds must be a positive integer")
    policy: dict[str, Any] = {"mode": mode, "maxRecordIds": max_ids}
    if mode == "sampled":
        if not isinstance(rate, int) or isinstance(rate, bool) or rate < 1:
            raise ValueError("sampled read audit needs a positive sample rate, e.g. sampled:10")
        policy["sampleRate"] = rate
    return policy


def _read_entries(sources: list[dict[str, Any]], policy: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Digest read sources; returns (entries, restricted). Content never leaves this function."""
    entries: list[dict[str, Any]] = []
    restricted = False
    for source in sources:
        source_id = source.get("sourceId")
        if not isinstance(source_id, str) or not source_id or len(source_id) > MAX_CONTEXT_VALUE:
            raise ValueError("Governed action failed: read source needs a bounded sourceId")
        classification = source.get("classification")
        records = source.get("records", [])
        if not isinstance(records, list):
            raise ValueError("Governed action failed: read source records must be a list")
        digested = []
        for record in records:
            record_id = record.get("id") if isinstance(record, dict) else None
            if not isinstance(record_id, str) or not record_id or len(record_id) > MAX_CONTEXT_VALUE:
                raise ValueError("Governed action failed: read record needs a bounded id")
            digested.append({"id": record_id, "contentDigest": _sha256_json(record.get("content"))})
        entry: dict[str, Any] = {"sourceId": source_id, "recordCount": len(digested), "recordsDigest": _sha256_json(digested)}
        if classification is not None:
            entry["classification"] = classification
        if "content" in source:
            entry["contentDigest"] = _sha256_json(source["content"])
        is_restricted = classification == "restricted"
        restricted = restricted or is_restricted
        if policy["mode"] != "aggregate" or is_restricted:
            limit = len(digested) if is_restricted else policy["maxRecordIds"]
            entry["records"] = digested[:limit]
            if len(digested) > limit:
                entry["recordsTruncated"] = True
        entries.append(entry)
    return entries, restricted


def make_read_event(
    request: dict[str, Any],
    sources: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a read event recording what was read (IDs and digests), or None when sampled out.

    Restricted-classification sources are always recorded, with every record ID, whatever
    the sampling or aggregation policy says.
    """
    policy = policy or parse_read_policy(None)
    entries, restricted = _read_entries(sources, policy)
    if policy["mode"] == "sampled" and not restricted:
        if int(request["requestId"][:8], 16) % policy["sampleRate"] != 0:
            return None
    details = {"reads": entries, "readPolicy": policy["mode"]}
    return make_audit_event("read", request, outcome="recorded", details=details, ledger=ledger)


def record_knowledge_read(
    desired_state: dict[str, Any],
    request: dict[str, Any],
    source_name: str,
    documents: list[dict[str, Any]],
    *,
    audit_path: pathlib.Path | None,
    policy: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ledger a ``kb.search`` style read of one selected KnowledgeSource by document digest."""
    selected = {
        item.get("metadata", {}).get("name"): item
        for item in desired_state.get("knowledge", [])
        if isinstance(item, dict)
    }
    knowledge = selected.get(source_name)
    if knowledge is None:
        raise PermissionError(f"KnowledgeSource {source_name!r} is not selected by Agent {request['agent']!r}")
    classification = knowledge.get("spec", {}).get("classification")
    source = {"sourceId": f"knowledge:{source_name}", "classification": classification, "records": documents}
    event = make_read_event(request, [source], policy=policy, ledger=ledger)
    if event is not None:
        append_audit_event(audit_path, event)
    return event


def _last_audit_event(handle: Any) -> tuple[dict[str, Any] | None, int]:
    handle.seek(0)
    last: dict[str, Any] | None = None
    count = 0
    for raw in handle:
        if not raw.strip():
            continue
        count += 1
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Governed action audit contains a non-object JSONL record")
        last = value
    return last, count


def _event_chain_hash(event: dict[str, Any]) -> str:
    material = dict(event)
    material.pop("eventHash", None)
    return _sha256_json(material)


def append_audit_event(path: pathlib.Path | None, event: dict[str, Any]) -> None:
    """Append one durable, tamper-evident audit event under an exclusive file lock."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=False) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            last, count = _last_audit_event(handle)
            previous_hash: str | None = None
            if last is not None:
                stored = last.get("eventHash")
                previous_hash = stored if isinstance(stored, str) and stored else _sha256_json(last)
            chained = dict(event)
            chained["sequence"] = count + 1
            chained["previousEventHash"] = previous_hash
            chained["eventHash"] = _event_chain_hash(chained)
            handle.seek(0, os.SEEK_END)
            handle.write(_canonical_json(chained) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _verify_audit_chain_handle(handle: Any) -> dict[str, Any]:
    """Validate one already-opened, caller-locked ledger descriptor."""
    handle.seek(0)
    previous_hash: str | None = None
    expected_sequence = 1
    events = 0
    last_version = 1
    for line_number, raw in enumerate(handle, start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Audit chain invalid at line {line_number}: invalid JSON") from exc
        if not isinstance(event, dict):
            raise ValueError(f"Audit chain invalid at line {line_number}: record is not an object")
        events += 1
        schema_version = event.get("schemaVersion", 1)
        if type(schema_version) is not int or schema_version not in (1, *CHAINED_SCHEMA_VERSIONS):
            raise ValueError(f"Audit chain invalid at line {line_number}: unsupported schemaVersion {schema_version!r}")
        if schema_version < last_version:
            raise ValueError(f"Audit chain invalid at line {line_number}: schemaVersion moved backwards")
        last_version = schema_version
        if schema_version == 1:
            previous_hash = _sha256_json(event)
            expected_sequence = events + 1
            continue
        if event.get("sequence") != expected_sequence:
            raise ValueError(f"Audit chain invalid at line {line_number}: sequence mismatch")
        if event.get("previousEventHash") != previous_hash:
            raise ValueError(f"Audit chain invalid at line {line_number}: previous hash mismatch")
        stored_hash = event.get("eventHash")
        if not isinstance(stored_hash, str) or stored_hash != _event_chain_hash(event):
            raise ValueError(f"Audit chain invalid at line {line_number}: event hash mismatch")
        previous_hash = stored_hash
        expected_sequence += 1
    return {"valid": True, "events": events, "headHash": previous_hash}


def verify_audit_chain(path: pathlib.Path) -> dict[str, Any]:
    """Verify ordering and SHA-256 chaining for chained (schema-v2 and v3) audit events.

    Legacy schema-v1 records may precede chained records. Their canonical digest becomes
    the anchor for the first chained record, so migration is detectable without rewriting
    history. Schema versions may only move forward along the chain.
    """
    if not path.exists():
        return {"valid": True, "events": 0, "headHash": None}
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _verify_audit_chain_handle(handle)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def _result_receipt(result: Any) -> dict[str, Any]:
    encoded = _canonical_json(result)
    return {
        "resultType": type(result).__name__,
        "resultDigest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _open_handoff(
    request: dict[str, Any],
    store: pathlib.Path | None,
    *,
    family: str | None,
    reason: str,
    assignee: str | None,
    deadline_hours: int | None,
    audit: Callable[..., None],
) -> dict[str, Any]:
    """Prepare the exact action for a named person (Tier 4); the action itself is never executed."""
    if store is None:
        audit("handoff", "failed", {"reason": "no-handoff-store"}, {"tier": 4, "provider": "human"})
        raise PermissionError(f"Tool {request['tool']!r} must be performed by a person and no hand-off inbox is configured")
    if not isinstance(assignee, str) or not assignee.strip():
        audit("handoff", "failed", {"reason": "no-assignee"}, {"tier": 4, "provider": "human"})
        raise PermissionError(f"Tool {request['tool']!r} must be performed by a person but no named person is configured")
    existing = HANDOFF.find(store, request["requestId"])
    document = HANDOFF.create(
        store,
        request,
        capability=family,
        reason=reason,
        assignee=assignee,
        deadline_hours=deadline_hours or HANDOFF.DEFAULT_DEADLINE_HOURS,
    )
    if existing is None or existing.get("status") != "pending":
        audit(
            "handoff",
            "created",
            {"reason": reason, "assignee": document["assignee"], "handoffId": document["id"], "deadline": document["deadline"]},
            {"tier": 4, "provider": "human"},
        )
    return {"status": "handed-off", "request": request, "handoff": HANDOFF.describe(document)}


def create_handoff(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    store: pathlib.Path | None,
    *,
    reason: str,
    assignee: str | None = None,
    deadline_hours: int | None = None,
    initiating_user: str | None = None,
    governance_context: dict[str, Any] | None = None,
    audit_path: pathlib.Path | None = None,
    ledger: dict[str, Any] | None = None,
    principal_access: str | None = None,
) -> dict[str, Any]:
    """Hand an unbound or unavailable capability's action to a person instead of failing.

    The Tool must be selected and the policy must not deny it for this principal (a public
    principal therefore cannot hand off write actions). Deny rules and the public-principal
    floor still apply; approval is not needed because nothing is executed automatically.
    """
    authorization = authorize_action(
        desired_state, tool_name, arguments,
        initiating_user=initiating_user, governance_context=governance_context, principal_access=principal_access,
        audit_path=audit_path,
    )
    request = authorization["request"]
    base_ledger: dict[str, Any] = {
        "policyRuleId": authorization.get("ruleId") or authorization["reason"],
        "policyVersion": POLICY.policy_version(desired_state.get("policies")),
    }
    family = CAPABILITIES.family_of(tool_name)
    if family is not None:
        base_ledger["capability"] = family
    base_ledger.update(ledger or {})
    base_ledger = ledger_context(base_ledger)

    def audit(event_type: str, outcome: str, details: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> None:
        append_audit_event(
            audit_path,
            make_audit_event(event_type, request, outcome=outcome, details=details, ledger={**base_ledger, **(extra or {})}),
        )

    if authorization["decision"] == "deny":
        audit("authorization", "deny", {"reason": authorization["reason"]})
        raise PermissionError(f"Tool {tool_name!r} cannot be handed off: {authorization['reason']}")
    if authorization["decision"] == "hand-off":  # a human-only rule names its own assignee and deadline
        reason = "human-only"
        assignee = authorization.get("assignee") or assignee
        deadline_hours = authorization.get("deadlineHours") or deadline_hours
    audit("authorization", "hand-off", {"reason": reason})
    return _open_handoff(
        request, store, family=family, reason=reason, assignee=assignee, deadline_hours=deadline_hours, audit=audit
    )


def complete_handoff(
    store: pathlib.Path,
    handoff_id: str,
    outcome: str,
    by: str,
    *,
    audit_path: pathlib.Path | None = None,
    note: str | None = None,
    via: str | None = None,
) -> dict[str, Any]:
    """Close a hand-off as done/declined by a named person and ledger it as a tier-4 action."""
    document = HANDOFF.complete(store, handoff_id, outcome, by, note=note, via=via)
    ledger: dict[str, Any] = {"tier": 4, "provider": "human"}
    if document.get("capability"):
        ledger["capability"] = document["capability"]
    # A human task step (#97) is stored as a hand-off whose request has no Tool arguments and may have no Agent name.
    request = {**document["request"], "agent": document["request"].get("agent") or "unassigned",
               "arguments": document["request"].get("arguments", {})}
    event = make_audit_event(
        "handoff",
        request,
        outcome=document["status"],
        details={
            "handoffId": document["id"], "completedBy": document["completedBy"], "late": bool(document.get("late")),
            **({"via": document["completedVia"]} if document.get("completedVia") else {}),
        },
        ledger=ledger,
    )
    append_audit_event(audit_path, event)
    return document


def execute_action(
    desired_state: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    executor: Callable[[dict[str, Any]], Any],
    *,
    approval: dict[str, Any] | None = None,
    initiating_user: str | None = None,
    governance_context: dict[str, Any] | None = None,
    audit_path: pathlib.Path | None = None,
    ledger: dict[str, Any] | None = None,
    principal_access: str | None = None,
    read_receipt: Callable[[Any], list[dict[str, Any]]] | None = None,
    read_policy: dict[str, Any] | None = None,
    external_object_ref: Callable[[Any], str | None] | None = None,
    handoff_store: pathlib.Path | None = None,
    handoff_assignee: str | None = None,
) -> dict[str, Any]:
    authorization = authorize_action(
        desired_state,
        tool_name,
        arguments,
        initiating_user=initiating_user,
        governance_context=governance_context,
        principal_access=principal_access,
        audit_path=audit_path,
    )
    request = authorization["request"]
    base_ledger: dict[str, Any] = {
        "policyRuleId": authorization.get("ruleId") or authorization["reason"],
        "policyVersion": POLICY.policy_version(desired_state.get("policies")),
    }
    family = CAPABILITIES.family_of(tool_name)
    if family is not None:
        base_ledger["capability"] = family
    if principal_access is not None:
        base_ledger["principalAccess"] = principal_access  # lets a later policy replay use the access level that was really applied
    base_ledger.update(ledger or {})
    base_ledger = ledger_context(base_ledger)

    def audit(event_type: str, outcome: str, details: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> None:
        event_ledger = {**base_ledger, **(extra or {})}
        append_audit_event(audit_path, make_audit_event(event_type, request, outcome=outcome, details=details, ledger=event_ledger))

    audit("authorization", authorization["decision"], {"reason": authorization["reason"]})

    if authorization["decision"] == "deny":
        if authorization["reason"] == "principal-not-authorized":
            raise PermissionError(f"Tool {tool_name!r} requires a mapped, verified principal")
        if authorization["reason"] == "policy-deny":
            raise PermissionError(f"Tool {tool_name!r} is denied by policy rule {authorization['ruleId']!r}")
        raise PermissionError(f"Tool {tool_name!r} is not selected by Agent {request['agent']!r}")

    if authorization["decision"] == "hand-off":
        # Policy forbids automation here: nothing is executed and no approval can change that.
        return _open_handoff(
            request,
            handoff_store,
            family=family,
            reason="human-only",
            assignee=authorization.get("assignee") or handoff_assignee,
            deadline_hours=authorization.get("deadlineHours"),
            audit=audit,
        )

    if authorization["decision"] == "require-approval":
        if approval is None:
            audit("approval", "pending")
            return {"status": "pending-approval", "request": request}
        if approval.get("requestId") != request["requestId"]:
            audit("approval", "rejected", {"reason": "request-id-mismatch"})
            raise PermissionError("Approval does not match the exact material action request")
        if approval.get("approved") is not True:
            audit("approval", "rejected", {"reason": "not-approved"})
            raise PermissionError("Action approval was not granted")
        approver = approval.get("approver")
        if not isinstance(approver, str) or not approver:
            raise ValueError("Approved action must record a non-empty approver")
        audit("approval", "approved", {"approver": approver})

    try:
        result = executor(arguments)
    except Exception as exc:
        audit("execution", "failed", {"errorType": type(exc).__name__})
        raise
    extra: dict[str, Any] = {}
    if external_object_ref is not None:
        reference = external_object_ref(result)
        if reference:
            extra["externalObjectRef"] = reference
    audit("execution", "succeeded", _result_receipt(result), extra)
    if _tool_risk(desired_state, tool_name) == "read":
        sources = read_receipt(result) if read_receipt is not None else [{"sourceId": f"tool:{tool_name}", "content": result}]
        event = make_read_event(request, sources, policy=read_policy, ledger=base_ledger)
        if event is not None:
            append_audit_event(audit_path, event)
    return {"status": "executed", "request": request, "result": result}


def record_policy_change(
    path: pathlib.Path | None,
    before: list[dict[str, Any]] | None,
    after: list[dict[str, Any]] | None,
    *,
    changed_by: str,
    agent: str | None = None,
) -> dict[str, Any] | None:
    """Ledger a policy change with who made it and the rule-level diff; no-op when nothing changed.

    ``agent`` names the Agent whose policy changed when one environment serves several (a team runtime); omitted otherwise.
    """
    if not isinstance(changed_by, str) or not changed_by.strip():
        raise ValueError("Governed action failed: a policy change must record who made it")
    diff = POLICY.diff_policies(before, after)
    if not (diff["added"] or diff["removed"] or diff["modified"]):
        return None
    event = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "timestamp": _utc_timestamp(),
        "event": "policy-change",
        "outcome": "recorded",
        "changedBy": changed_by.strip()[:MAX_CONTEXT_VALUE],
        "details": diff,
    }
    if agent:
        event["agent"] = agent
    append_audit_event(path, event)
    return event


def record_operator_action(
    path: pathlib.Path | None,
    action: str,
    performed_by: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ledger an action a person performed on the runtime (for example erasing a contact).

    ``details`` should hold identifiers and counts only; never personal data or record contents.
    """
    if not isinstance(performed_by, str) or not performed_by.strip():
        raise ValueError("Governed action failed: an operator action must record who performed it")
    if not isinstance(action, str) or not action.strip() or len(action) > MAX_CONTEXT_VALUE:
        raise ValueError("Governed action failed: an operator action needs a short action name")
    event = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "timestamp": _utc_timestamp(),
        "event": "operator-action",
        "outcome": "recorded",
        "action": action.strip(),
        "performedBy": performed_by.strip()[:MAX_CONTEXT_VALUE],
        "details": details or {},
    }
    append_audit_event(path, event)
    return event


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("desired_state", help="Compiled single-Agent desired-state JSON")
    parser.add_argument("tool", help="Tool metadata.name")
    parser.add_argument("--arguments", default="{}", help="Action arguments as a JSON object")
    parser.add_argument("--initiating-user", help="Optional initiating user/workload identity")
    parser.add_argument("--governance-context", default="{}", help="First-party governance context as a JSON object")
    args = parser.parse_args()

    try:
        desired_state = _load_json(pathlib.Path(args.desired_state))
        arguments = json.loads(args.arguments)
        governance_context = json.loads(args.governance_context)
        if not isinstance(arguments, dict):
            raise ValueError("--arguments must decode to a JSON object")
        if not isinstance(governance_context, dict):
            raise ValueError("--governance-context must decode to a JSON object")
        result = authorize_action(desired_state, args.tool, arguments, initiating_user=args.initiating_user, governance_context=governance_context)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["decision"] != "deny" else 2


if __name__ == "__main__":
    raise SystemExit(main())
