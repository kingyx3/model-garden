"""Client-owned approval policy for the governed action boundary.

A Policy resource holds ordered rules (`allow`, `deny`, `require-approval`, `human-only`)
over Tool names, data classifications, channels and principal types. Evaluation is
deliberately small and fail-closed:

- a matching `deny` rule always wins;
- a matching `human-only` rule forbids automation: the action is prepared for the rule's named
  assignee (Tier 4 hand-off) instead of being executed, and no approval can turn it into execution;
- the Agent's ``approvals.externalActions: required`` switch and any matching
  `require-approval` rule can never be relaxed by an `allow` rule (the stricter source wins);
- an `allow` rule may waive only a Tool's own ``approvalRequired`` default, and only when it
  carries a valid client-owner sign-off bound to the rule's exact content;
- an `allow` rule applies to member principals unless it names ``public`` explicitly, and a
  public principal can reach at most one signed, named ``write`` Tool;
- ``privileged`` Tools are never auto-approved and never reachable by a public principal;
- an `allow` rule may carry bounded ``conditions`` (an argument allow-list, a per-day usage
  limit for the Tool and a per-day limit for each requester). Conditions only narrow a loosening
  rule: when one is unmet, or cannot be checked, the rule simply does not apply and the stricter
  default stands. They are part of the rule's content, so the owner's sign-off digest binds them too.

With no Policy the result is exactly the previous Agent/Tool behaviour, plus the privileged
floor. This module owns no I/O and no credentials.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

EFFECTS = ("allow", "deny", "require-approval", "human-only")
MEMBER = "member"
PUBLIC = "public"
DEFAULT_VERSION = "model-garden-governance/1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rule_digest(rule: Mapping[str, Any]) -> str:
    """Digest of a rule's content excluding its sign-off, so a signature binds what it approves."""
    return hashlib.sha256(_canonical({k: v for k, v in rule.items() if k != "signoff"}).encode("utf-8")).hexdigest()


def rules_of(policies: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten Policy resources into one ordered rule list (policy order, then rule order)."""
    flattened: list[dict[str, Any]] = []
    for policy in policies or []:
        for rule in policy.get("spec", {}).get("rules", []):
            flattened.append(dict(rule))
    return flattened


def policy_version(policies: Iterable[Mapping[str, Any]] | None) -> str:
    """Stable identifier of the effective rules; the legacy constant when there are none."""
    rules = rules_of(policies)
    if not rules:
        return DEFAULT_VERSION
    return "policy:" + hashlib.sha256(_canonical(rules).encode("utf-8")).hexdigest()[:16]


def signoff_valid(rule: Mapping[str, Any]) -> bool:
    signoff = rule.get("signoff")
    return (
        isinstance(signoff, Mapping)
        and isinstance(signoff.get("owner"), str)
        and bool(signoff["owner"].strip())
        and signoff.get("ruleDigest") == rule_digest(rule)
    )


SIGNATURE_CONTEXT = b"model-garden-policy-signoff/1\n"


def _ed25519():
    from cryptography.hazmat.primitives.asymmetric import ed25519  # imported here so a deployment without signing never needs it

    return ed25519


def _signed_message(owner: str, digest: str) -> bytes:
    return SIGNATURE_CONTEXT + owner.strip().lower().encode("utf-8") + b"\n" + digest.encode("ascii")


def generate_owner_key() -> dict[str, str]:
    """A new owner key pair as base64 raw bytes. The private half stays with the owner; only the public half is configured."""
    from cryptography.hazmat.primitives import serialization

    private = _ed25519().Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw, serialization.PublicFormat.Raw
    return {
        "private": base64.b64encode(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode(),
        "public": base64.b64encode(private.public_key().public_bytes(*raw)).decode(),
    }


def sign_rule(rule: Mapping[str, Any], owner: str, private_key: str) -> dict[str, Any]:
    """The rule with the owner's sign-off: their name, the exact content digest and an Ed25519 signature over both."""
    digest = rule_digest(rule)
    try:
        key = _ed25519().Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key, validate=True))
    except (ValueError, binascii.Error):
        raise ValueError("the owner private key must be a base64 Ed25519 key") from None
    signature = base64.b64encode(key.sign(_signed_message(owner, digest))).decode()
    return {**{k: v for k, v in rule.items() if k != "signoff"}, "signoff": {"owner": owner, "ruleDigest": digest, "signature": signature}}


def signature_valid(rule: Mapping[str, Any], public_key: str) -> bool:
    """True only when the rule's sign-off carries a signature by this public key over the owner and the rule's exact content."""
    signoff = rule.get("signoff")
    if not signoff_valid(rule) or not isinstance(signoff.get("signature"), str):
        return False
    try:
        key = _ed25519().Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signoff["signature"], validate=True), _signed_message(signoff["owner"], rule_digest(rule)))
    except Exception:  # noqa: BLE001 - any failure to verify is a refusal
        return False
    return True


def owner_keys_from_binding(binding: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The environment's owner public keys (``governance.ownerKeys``: identity to base64 Ed25519 key), or ``None`` when it names none.

    When present, a relaxing rule must be signed with the named owner's key, so a name typed into a client-editable workspace is no
    longer enough. The keys are public; the private halves never enter the workspace or the deployment.
    """
    if not isinstance(binding, Mapping):
        return None
    spec = binding.get("spec", binding) if binding.get("kind") == "EnvironmentBinding" else binding
    governance = spec.get("governance") if isinstance(spec, Mapping) else None
    keys = governance.get("ownerKeys") if isinstance(governance, Mapping) else None
    if keys is None:
        return None
    if not isinstance(keys, Mapping):
        raise ValueError("EnvironmentBinding governance.ownerKeys must map each owner identity to a base64 Ed25519 public key")
    normal: dict[str, str] = {}
    for identity, value in keys.items():
        try:
            valid = isinstance(identity, str) and identity.strip() and isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32
        except (ValueError, binascii.Error):
            valid = False
        if not valid:
            raise ValueError("EnvironmentBinding governance.ownerKeys must map each owner identity to a base64 Ed25519 public key")
        normal[identity.strip().lower()] = value
    return normal


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def conditions_status(
    rule: Mapping[str, Any],
    arguments: Mapping[str, Any] | None,
    usage_today: int | None,
    usage_by_principal: int | None = None,
) -> str | None:
    """None when a rule's bounded conditions hold, else why the rule does not apply.

    Fails closed: an argument the call did not supply, a non-scalar value, or a usage count the
    caller could not provide all make the condition unmet. ``maxPerDay`` bounds the Tool's
    executions for everyone; ``maxPerPrincipalPerDay`` bounds each requester's own, so one person
    (or one public sender) cannot use up the allowance the others rely on. Both must hold.
    """
    conditions = rule.get("conditions")
    if not isinstance(conditions, Mapping):
        return None
    allowed = conditions.get("arguments")
    if isinstance(allowed, Mapping):
        supplied = arguments if isinstance(arguments, Mapping) else {}
        for name, values in allowed.items():
            value = supplied.get(name)
            if not _scalar(value) or not any(type(value) is type(item) and value == item for item in values):
                return "argument-condition"
    limit = conditions.get("maxPerDay")
    if limit is not None and (usage_today is None or usage_today >= limit):
        return "usage-limit"
    per_principal = conditions.get("maxPerPrincipalPerDay")
    if per_principal is not None and (usage_by_principal is None or usage_by_principal >= per_principal):
        return "principal-usage-limit"
    return None


def _action_matches(rule: Mapping[str, Any], tool_name: str) -> bool:
    patterns = rule.get("actions") or []
    return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns)


def _matches(
    rule: Mapping[str, Any],
    *,
    tool_name: str,
    classification: str | None,
    channel: str | None,
    principal: str,
) -> bool:
    effect = rule.get("effect")
    restrictive = effect != "allow"
    if not _action_matches(rule, tool_name) and not (restrictive and not rule.get("actions")):
        return False
    classes = rule.get("dataClassifications") or []
    if classes and (classification not in classes if classification is not None else not restrictive):
        return False
    channels = rule.get("channels") or []
    if channels and (channel not in channels if channel is not None else not restrictive):
        return False
    principals = rule.get("principals") or []
    if principals:
        return principal in principals
    # A restrictive rule covers every principal; a loosening rule covers members only.
    return restrictive or principal == MEMBER


def evaluate(
    policies: Iterable[Mapping[str, Any]] | None,
    *,
    tool_name: str,
    risk: str | None,
    classification: str | None = None,
    channel: str | None = None,
    principal: str | None = None,
    agent_requires_approval: bool = False,
    tool_requires_approval: bool = False,
    arguments: Mapping[str, Any] | None = None,
    usage_today: int | None = None,
    usage_by_principal: int | None = None,
    owner_keys: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return ``{"decision", "reason", "ruleId"}`` for one already-selected Tool call.

    ``arguments``, ``usage_today`` (successful executions of this Tool so far today, UTC) and
    ``usage_by_principal`` (those the requesting identity made) feed the bounded conditions of
    `allow` rules; omit them and conditional rules do not apply. When ``owner_keys`` is
    provided, approval-relaxing waivers require a valid owner Ed25519 signature at
    runtime as well as the content digest.
    """
    who = principal or MEMBER
    matched = []
    limited: dict[str, Any] | None = None  # first allow rule set aside only because its daily limit is used up
    for rule in rules_of(policies):
        if not _matches(rule, tool_name=tool_name, classification=classification, channel=channel, principal=who):
            continue
        if rule["effect"] == "allow":
            unmet = conditions_status(rule, arguments, usage_today, usage_by_principal)
            if unmet is not None:
                # Only a count that was actually proven and reached its limit is reported as "limit reached".
                proven = usage_today if unmet == "usage-limit" else usage_by_principal
                if unmet in ("usage-limit", "principal-usage-limit") and proven is not None and limited is None:
                    limited = rule
                continue
        matched.append(rule)
    denied = next((rule for rule in matched if rule["effect"] == "deny"), None)
    if denied is not None:
        return {"decision": "deny", "reason": "policy-deny", "ruleId": denied["id"]}
    allows = [rule for rule in matched if rule["effect"] == "allow"]
    # A digest/name alone is not an authenticated owner waiver. When a deployment
    # carries its trusted public-key binding, reverify the Ed25519 signature at the
    # execution boundary, not only when the workspace was compiled. An empty or
    # malformed key set grants no waiver; legacy unbound states retain their
    # existing approval behavior until the operator configures owner keys.
    def signed_waiver(rule: Mapping[str, Any]) -> bool:
        if not signoff_valid(rule):
            return False
        if owner_keys is None:
            return True
        if not isinstance(owner_keys, Mapping):
            return False
        owner = rule["signoff"]["owner"].strip().lower()
        key = owner_keys.get(owner)
        return isinstance(key, str) and signature_valid(rule, key)

    waiver = next((rule for rule in allows if signed_waiver(rule)), None)

    if who == PUBLIC and risk != "read":
        # Floor: a public principal never reaches privileged or external-communication
        # Tools, and reaches a write Tool only through an explicit, signed rule.
        if risk != "write" or waiver is None or PUBLIC not in (waiver.get("principals") or []):
            return {"decision": "deny", "reason": "principal-not-authorized", "ruleId": None}

    human_only = next((rule for rule in matched if rule["effect"] == "human-only"), None)
    if human_only is not None:
        # Automation is forbidden for this action: prepare it for a named person instead.
        return {
            "decision": "hand-off",
            "reason": "policy-human-only",
            "ruleId": human_only["id"],
            "assignee": human_only.get("assignee"),
            "deadlineHours": human_only.get("deadlineHours"),
        }
    if agent_requires_approval:
        return {"decision": "require-approval", "reason": "agent-requires-approval", "ruleId": None}
    required = next((rule for rule in matched if rule["effect"] == "require-approval"), None)
    if required is not None:
        return {"decision": "require-approval", "reason": "policy-require-approval", "ruleId": required["id"]}
    if risk == "privileged":
        return {"decision": "require-approval", "reason": "privileged-requires-approval", "ruleId": None}
    if tool_requires_approval and waiver is None:
        if limited is not None:
            return {"decision": "require-approval", "reason": "policy-limit-reached", "ruleId": limited["id"]}
        return {"decision": "require-approval", "reason": "tool-requires-approval", "ruleId": None}
    if waiver is not None:
        return {"decision": "allow", "reason": "policy-allow", "ruleId": waiver["id"]}
    if allows:
        return {"decision": "allow", "reason": "policy-allow", "ruleId": allows[0]["id"]}
    return {"decision": "allow", "reason": "selected-tool", "ruleId": None}


def owners_from_binding(binding: Mapping[str, Any] | None) -> list[str] | None:
    """The environment's named owners (``governance.owners``), or ``None`` when it names none.

    An empty list is meaningful: it names nobody, so every relaxing rule is refused.
    """
    if not isinstance(binding, Mapping):
        return None
    spec = binding.get("spec", binding) if binding.get("kind") == "EnvironmentBinding" else binding
    governance = spec.get("governance") if isinstance(spec, Mapping) else None
    owners = governance.get("owners") if isinstance(governance, Mapping) else None
    if owners is None:
        return None
    if not isinstance(owners, list) or not all(isinstance(item, str) and item.strip() for item in owners):
        raise ValueError("EnvironmentBinding governance.owners must be a list of non-empty identities")
    return sorted({item.strip().lower() for item in owners})


def relaxation_errors(
    policies: Iterable[Mapping[str, Any]] | None,
    tools: Iterable[Mapping[str, Any]],
    trusted_owners: Iterable[str] | None = None,
    owner_keys: Mapping[str, str] | None = None,
) -> list[str]:
    """Deploy-time checks: rule ids are unique, and every approval-relaxing rule is signed.

    An `allow` rule relaxes approval when it matches a selected Tool that is not read-class.
    Its sign-off must name the client owner and carry the digest of the rule's exact content,
    so editing a rule after it was approved blocks compilation until the owner re-signs.

    When ``trusted_owners`` is given (the environment's ``governance.owners``), the sign-off must also
    name one of them. The digest proves the rule was not edited after signing, and this list narrows who
    may claim to be the owner; neither authenticates the signer's identity. When ``owner_keys`` is given
    (``governance.ownerKeys``), the sign-off must also carry an Ed25519 signature by that owner's key, which does.
    """
    trusted = None if trusted_owners is None else {item.strip().lower() for item in trusted_owners}
    errors: list[str] = []
    seen: set[str] = set()
    selected = [
        (tool.get("metadata", {}).get("name", ""), tool.get("spec", {}).get("risk"))
        for tool in tools
    ]
    for rule in rules_of(policies):
        rule_id = rule["id"]
        if rule_id in seen:
            errors.append(f"Policy rule id {rule_id!r} is defined more than once")
        seen.add(rule_id)
        if rule["effect"] == "human-only" and not (isinstance(rule.get("assignee"), str) and rule["assignee"].strip()):
            errors.append(f"Policy human-only rule {rule_id!r} must name the person (assignee) who performs the action")
        if rule["effect"] != "allow":
            if rule.get("conditions") is not None:
                errors.append(
                    f"Policy rule {rule_id!r} has conditions but is not an allow rule; "
                    "conditions may only narrow a loosening rule"
                )
            continue
        if not rule.get("actions"):
            errors.append(f"Policy allow rule {rule_id!r} must list the actions it allows")
            continue
        relaxes = any(risk != "read" and _action_matches(rule, name) for name, risk in selected)
        if PUBLIC in (rule.get("principals") or []):
            relaxes = True
            risky = [
                name for name, risk in selected
                if _action_matches(rule, name) and risk not in ("read", "write")
            ]
            if risky:
                errors.append(
                    f"Policy rule {rule_id!r} would let a public principal reach "
                    f"{', '.join(sorted(risky))}; only read and write Tools may be named for public principals"
                )
        if relaxes and not signoff_valid(rule):
            errors.append(
                f"Policy rule {rule_id!r} relaxes an approval requirement and needs the client owner's "
                f"sign-off: signoff.owner plus signoff.ruleDigest {rule_digest(rule)}"
            )
        elif relaxes and trusted is not None and rule["signoff"]["owner"].strip().lower() not in trusted:
            errors.append(
                f"Policy rule {rule_id!r} is signed by {rule['signoff']['owner'].strip()!r}, who is not a named "
                "owner of this environment (governance.owners in the EnvironmentBinding)"
            )
        elif relaxes and owner_keys is not None:
            owner = rule["signoff"]["owner"].strip().lower()
            if owner not in owner_keys or not signature_valid(rule, owner_keys[owner]):
                errors.append(
                    f"Policy rule {rule_id!r} needs signoff.signature, made with the key governance.ownerKeys lists for "
                    f"{rule['signoff']['owner'].strip()!r} over the rule's current content (sign it with scripts/policy-signoff.py)"
                )
    return errors


def diff_policies(
    before: Iterable[Mapping[str, Any]] | None, after: Iterable[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Describe a policy change as rule ids, flagging the ones that loosen control."""
    old = {rule["id"]: rule for rule in rules_of(before)}
    new = {rule["id"]: rule for rule in rules_of(after)}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(
        rule_id for rule_id in set(old) & set(new)
        if _canonical(old[rule_id]) != _canonical(new[rule_id])
    )
    relaxed = sorted(
        {rule_id for rule_id in added + modified if new[rule_id]["effect"] == "allow"}
        | {rule_id for rule_id in removed + modified if old[rule_id]["effect"] != "allow"}
    )
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "relaxed": relaxed,
        "previousVersion": policy_version(before),
        "newVersion": policy_version(after),
    }


def preview(
    policies: Iterable[Mapping[str, Any]] | None,
    *,
    tool_name: str,
    risk: str | None,
    classification: str | None = None,
    agent_requires_approval: bool = False,
    tool_requires_approval: bool = False,
) -> dict[str, str]:
    """Outcome per principal type for the readiness/onboarding preview (channel-agnostic)."""
    outcome: dict[str, str] = {}
    for who in (MEMBER, PUBLIC):
        result = evaluate(
            policies,
            tool_name=tool_name,
            risk=risk,
            classification=classification,
            principal=who,
            agent_requires_approval=agent_requires_approval,
            tool_requires_approval=tool_requires_approval,
        )
        outcome[who] = {
            "allow": "automatic", "require-approval": "approval", "deny": "denied", "hand-off": "human",
        }[result["decision"]]
        if outcome[who] == "approval" and any(
            rule.get("effect") == "allow" and rule.get("conditions") and _action_matches(rule, tool_name)
            and (who in (rule.get("principals") or [MEMBER]))
            for rule in rules_of(policies)
        ):
            # Some calls (matching arguments, inside the daily limit) run automatically; others still need approval.
            outcome[who] = "conditional"
    return outcome
