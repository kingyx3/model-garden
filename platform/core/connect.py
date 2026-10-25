"""The guided connect step: one named coworker, exact scopes, a read-only test, then a grant of its own.

Connecting a CRM, mailbox or calendar must give the coworker least-privilege access without hand-edited bindings or credentials
in chat (#110). ``client-operator.py connect`` already lists providers and previews scopes; this module is the credential-free
core of the step that follows. It handles **no credentials**: the operator provisions the provider-side identity and secret
elsewhere and passes only the ``secret://`` *reference*. It does not run owner OAuth consent, store a secret, call a provider
itself (the read-only test is injected) or edit a file; it is not wired to the interactive CLI.

- **One coworker, named first.** A plan is for a specific Agent, a named client owner and a named operator, so a second coworker
  connecting the same system gets a grant of its own (``provider_grants.py``, #129) and never inherits the first one's.
- **Exact scopes.** The scopes come from the registry's reviewed mapping for the selected portable Tools. A request for more is
  refused. The plan has a digest of everything the owner is asked to consent to; ``apply`` needs that digest back, so consent
  to one plan cannot be reused for another.
- **Only shipped connectors.** A provider that is not ``built-credential-free`` or ``live`` (anything planned or in development)
  and a Tier 0 provider (which needs no credentials) are not connectable; the plan says why instead of asking for credentials.
- **Shared or delegated mailboxes.** A mailbox is connected as ``shared`` or ``delegated``; a ``personal`` mailbox needs an explicit
  owner exception, which is recorded.
- **A preview is not an authorization token.** Before the read-only test, recompute the exact plan from the current registry and refuse any stale, modified or no-longer-connectable provider, even if someone recalculates its unkeyed digest.\n- **The test comes first and failure changes nothing.** ``apply`` runs the injected read-only connection test before touching
  anything. If it fails, no grant is recorded, the binding is unchanged and the failure is ledgered; on success the grant is
  recorded and the binding fragment (provider, tier, scopes, owner, never a secret) is returned for the caller to merge.
- **Ledgered with both people.** Connect, failed connect and disconnect each call the audit hook naming the operator and the
  client owner, with identifiers, never a credential, reference or account.

- **Changing a working connection is explicit and reversible.** ``merge_binding`` refuses, unless the caller says so, to widen the
  scopes of an existing binding, change its owner or switch its provider; ``describe_change`` shows exactly what a fragment would
  change. ``replace`` connects the new grant first (the test must pass), writes the binding through an injected callable, and
  only then revokes the previous grant. If the write fails the new grant is revoked and the previous grant and binding are left
  as they were, and both outcomes are ledgered (``connection.replaced`` / ``connection.rolled-back``).

Not done here: owner OAuth consent and token acquisition, storing the credential, a live test, and re-running Skill
readiness after a change (the caller does that against the new binding).
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

MAILBOX_KINDS = ("shared", "delegated", "personal")
BINDING_KEYS = ("provider", "tier", "scopes", "owner")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
Audit = Callable[[str, str, dict[str, Any]], Any]


class ConnectError(ValueError):
    """Raised for an invalid plan or a connection that must not proceed; nothing is changed when it is raised."""


def _module(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _integrations():
    return _module("model_garden_connect_integrations", "integrations.py")


def _person(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PERSON.match(value):
        raise ConnectError(f"{label} must be a named person's identity, never a password or token")
    return value


def digest_of(plan: Mapping[str, Any]) -> str:
    material = {key: plan[key] for key in ("family", "provider", "tier", "agent", "owner", "operator", "tools", "scopes", "mailbox", "ownerException") if key in plan}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def plan(
    *,
    family: str,
    provider: str,
    tools: Iterable[str],
    agent: str,
    owner: str,
    operator: str,
    requested_scopes: Iterable[str] | None = None,
    mailbox: Mapping[str, Any] | None = None,
    owner_exception: str | None = None,
) -> dict[str, Any]:
    """The non-mutating plan the client owner is asked to consent to, with a digest of exactly what it covers."""
    if not isinstance(agent, str) or not _AGENT.match(agent):
        raise ConnectError("choose the specific coworker (Agent name) before connecting anything")
    owner, operator = _person(owner, "owner"), _person(operator, "operator")
    if family == "mail":
        kind = (mailbox or {}).get("kind")
        address = (mailbox or {}).get("address")
        if kind not in MAILBOX_KINDS or not isinstance(address, str) or "@" not in address:
            raise ConnectError("name the mailbox (address and kind: shared, delegated or personal)")
        if kind == "personal" and not (isinstance(owner_exception, str) and owner_exception.strip() and len(owner_exception) <= 200):
            raise ConnectError("a personal mailbox is refused unless the owner records an explicit exception")
    elif mailbox is not None:
        raise ConnectError("a mailbox only applies to the mail family")
    chosen = sorted(set(tools))
    try:
        base = _integrations().plan(family, provider, chosen)
    except ValueError as exc:
        raise ConnectError(str(exc)) from exc
    result: dict[str, Any] = {
        "family": family, "provider": provider, "tier": base["tier"], "status": base["status"], "agent": agent, "owner": owner,
        "operator": operator, "tools": chosen, "scopes": [],
    }
    if mailbox is not None:
        result["mailbox"] = {"address": mailbox["address"], "kind": mailbox["kind"]}
        if owner_exception:
            result["ownerException"] = owner_exception.strip()
    if base["tier"] == 0:
        result.update(connectable=False, reason="a built-in Tier 0 provider needs no credentials or grant; bind it directly")
    elif base["status"] not in _integrations().BUILT:
        result.update(connectable=False, reason=base["blocker"])
    else:
        needed = base["scopes"]
        if requested_scopes is not None:
            extra = sorted(set(requested_scopes) - set(needed))
            if extra:
                raise ConnectError("scopes beyond what the selected Tools need are refused: " + ", ".join(extra))
        result.update(connectable=True, scopes=needed, reason=None)
    result["digest"] = digest_of(result)
    return result


def _record(audit: Audit | None, action: str, by: str, details: dict[str, Any]) -> None:
    if audit is not None:
        audit(action, by, details)


def apply(
    proposal: Mapping[str, Any],
    registry: Any,
    *,
    owner_consent: str,
    connection_test: Callable[[], Mapping[str, Any]],
    connection_id: str,
    isolation: str,
    acting_identity: str,
    secret_ref: str,
    now: int,
    audit: Audit | None = None,
) -> dict[str, Any]:
    """Run the read-only test and, only if it passes, record the coworker's own grant and return the binding fragment."""
    # A digest is an owner-facing change detector, not a signed capability. Rebuild the
    # preview from the *current* reviewed registry before testing or creating a grant.
    # Otherwise a caller could flip connectable on a planned/Tier 0 provider, or
    # recompute the unkeyed digest after widening its requested scopes.
    try:
        expected = plan(
            family=proposal["family"], provider=proposal["provider"], tools=proposal["tools"],
            agent=proposal["agent"], owner=proposal["owner"], operator=proposal["operator"],
            mailbox=proposal.get("mailbox"), owner_exception=proposal.get("ownerException"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectError("connection plan is invalid or no longer approved; preview again") from exc
    if not expected["connectable"] or dict(proposal) != expected:
        raise ConnectError("connection plan is invalid or no longer approved; preview again")
    if not isinstance(owner_consent, str) or not _DIGEST.match(owner_consent) or owner_consent != proposal.get("digest") or digest_of(proposal) != proposal["digest"]:
        raise ConnectError("the owner's consent does not match this plan; show the owner the exact scopes and ask again")
    base = {"agent": proposal["agent"], "family": proposal["family"], "provider": proposal["provider"], "owner": proposal["owner"],
            "operator": proposal["operator"], "tools": list(proposal["tools"]), "consentDigest": proposal["digest"]}

    def failed(reason: str) -> ConnectError:
        _record(audit, "connection.connect-failed", proposal["operator"], {**base, "reason": reason})
        return ConnectError(f"connection not made ({reason}); nothing was changed")

    try:
        outcome = connection_test()
    except Exception as exc:  # noqa: BLE001 - any failure of the test is a failed connection
        raise failed(f"test-error:{type(exc).__name__}") from exc
    if not isinstance(outcome, Mapping) or outcome.get("ok") is not True:
        raise failed("test-failed")
    try:
        grant = registry.create(
            agent=proposal["agent"], family=proposal["family"], provider=proposal["provider"], connection_id=connection_id,
            isolation=isolation, acting_identity=acting_identity, secret_ref=secret_ref, scopes=proposal["tools"],
            owner=proposal["owner"], by=proposal["operator"], now=now,
        )
    except PermissionError as exc:  # the registry refused to share a credential or identity
        raise failed("grant-refused") from exc
    _record(audit, "connection.connect", proposal["operator"], {**base, "grantId": grant["id"], "tier": proposal["tier"], "connectionTest": "passed"})
    fragment = {"provider": proposal["provider"], "tier": proposal["tier"], "scopes": list(proposal["scopes"]), "owner": proposal["owner"]}
    return {"grant": grant, "binding": fragment}


def disconnect(
    registry: Any, grant_id: str, *, operator: str, owner: str, now: int, audit: Audit | None = None
) -> dict[str, Any]:
    """Revoke one coworker's grant (no other coworker is affected) and ledger who did it and for which owner."""
    operator, owner = _person(operator, "operator"), _person(owner, "owner")
    try:
        grant = registry.revoke(grant_id, by=operator, now=now)
    except PermissionError as exc:
        raise ConnectError(str(exc)) from exc
    _record(audit, "connection.disconnect", operator, {
        "agent": grant["agent"], "family": grant["family"], "provider": grant["provider"], "owner": owner, "operator": operator,
        "grantId": grant["id"],
    })
    return grant


def describe_change(existing: Mapping[str, Any] | None, fragment: Mapping[str, Any]) -> dict[str, Any]:
    """What applying ``fragment`` over an existing ``connectors.<family>`` entry would change (nothing secret is compared)."""
    if not isinstance(existing, Mapping):
        return {"new": True, "providerChanged": False, "ownerChanged": False, "broadened": [], "narrowed": []}
    # A legacy binding with no ``scopes`` key had no stated limit, so naming scopes for it narrows it; only a binding that already
    # lists scopes can be widened.
    scoped = "scopes" in existing
    old_scopes = {str(item) for item in existing.get("scopes") or ()}
    new_scopes = {str(item) for item in fragment.get("scopes") or ()} if scoped else old_scopes
    return {
        "new": False,
        "providerChanged": existing.get("provider") not in (None, fragment.get("provider")),
        "ownerChanged": existing.get("owner") not in (None, fragment.get("owner")),
        "broadened": sorted(new_scopes - old_scopes),
        "narrowed": sorted(old_scopes - new_scopes),
    }


def merge_binding(
    binding: Mapping[str, Any], family: str, fragment: Mapping[str, Any], *, replace: bool = False, rescope: bool = False
) -> dict[str, Any]:
    """A copy of the EnvironmentBinding with ``connectors.<family>`` set from a connect fragment; the input is never changed.

    Replacing the provider needs ``replace``; widening the scopes or changing the owner of an existing binding needs ``rescope``,
    so neither can happen by accident or through a refresh that quietly asks for more.
    """
    if set(fragment) - set(BINDING_KEYS):
        raise ConnectError("a binding fragment holds only provider, tier, scopes and owner; never a secret")
    if not isinstance(family, str) or not re.fullmatch(r"[a-z]+", family):
        raise ConnectError("family must be a lowercase capability family")
    merged = copy.deepcopy(dict(binding))
    connectors = merged.setdefault("connectors", {})
    existing = connectors.get(family)
    change = describe_change(existing, fragment)
    if change["providerChanged"] and not replace:
        raise ConnectError(f"{family} is already bound to {existing.get('provider')}; replacing a provider must be explicit")
    if not change["providerChanged"] and not rescope:
        if change["broadened"]:
            raise ConnectError(f"{family} would gain scopes ({', '.join(change['broadened'])}); widening access must be explicit")
        if change["ownerChanged"]:
            raise ConnectError(f"{family} would change owner; a change of owner must be explicit")
    kept: dict[str, Any] = {}
    if isinstance(existing, Mapping):
        same_provider = existing.get("provider") in (None, fragment.get("provider"))
        # An environment-level token_ref still serves the running sidecar until per-coworker resolution exists (#129), so it
        # stays for the same provider; it belonged to the old provider if the provider changes, so it goes then.
        kept = {key: value for key, value in existing.items() if key not in BINDING_KEYS and (same_provider or key != "token_ref")}
    connectors[family] = {**kept, **{key: fragment[key] for key in BINDING_KEYS if key in fragment}}
    return merged


def replace(
    proposal: Mapping[str, Any],
    registry: Any,
    *,
    previous_grant_id: str,
    write_binding: Callable[[Mapping[str, Any]], Any],
    owner_consent: str,
    connection_test: Callable[[], Mapping[str, Any]],
    connection_id: str,
    isolation: str,
    acting_identity: str,
    secret_ref: str,
    now: int,
    audit: Audit | None = None,
) -> dict[str, Any]:
    """Move a coworker to a new grant without ever leaving it with none.

    Order: the test and the new grant (``apply``; a failure changes nothing), then ``write_binding(fragment)``, then revoking
    the previous grant. If ``write_binding`` raises, the new grant is revoked and the previous grant stays in force; the
    binding was never changed by this function and the failure is ledgered. Revoking the previous grant after a successful
    write is retried by the caller if it fails: the binding already names the new grant's scopes, so the old one is only
    surplus access, and the result says so (``previousRevoked: False``).
    """
    previous = next((item for item in registry.list(agent=proposal.get("agent")) if item.get("id") == previous_grant_id), None)
    if previous is None or previous.get("revokedAt"):
        raise ConnectError("the grant being replaced is not an active grant of this coworker")
    if (previous.get("agent"), previous.get("family")) != (proposal.get("agent"), proposal.get("family")):
        raise ConnectError("a grant can only be replaced by a grant for the same coworker and capability")
    result = apply(
        proposal, registry, owner_consent=owner_consent, connection_test=connection_test, connection_id=connection_id,
        isolation=isolation, acting_identity=acting_identity, secret_ref=secret_ref, now=now, audit=audit,
    )
    base = {"agent": proposal["agent"], "family": proposal["family"], "provider": proposal["provider"], "owner": proposal["owner"],
            "operator": proposal["operator"], "previousGrantId": previous_grant_id, "newGrantId": result["grant"]["id"]}
    try:
        write_binding(result["binding"])
    except Exception as exc:  # noqa: BLE001 - whatever stops the write, the previous working grant must stay in force
        registry.revoke(result["grant"]["id"], by=proposal["operator"], now=now)
        _record(audit, "connection.rolled-back", proposal["operator"], {**base, "reason": f"binding-write:{type(exc).__name__}"})
        raise ConnectError(f"the binding could not be written ({type(exc).__name__}); the new grant was revoked and the previous one is unchanged") from exc
    revoked = True
    try:
        registry.revoke(previous_grant_id, by=proposal["operator"], now=now)
    except Exception:  # noqa: BLE001 - surplus access is reported, never hidden
        revoked = False
    _record(audit, "connection.replaced", proposal["operator"], {**base, "previousRevoked": revoked})
    return {**result, "previousGrantId": previous_grant_id, "previousRevoked": revoked}
