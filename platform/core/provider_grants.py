"""Per-coworker provider grants: each AI coworker authenticates to a connected system with its own identity.

A client security owner has to be able to scope, attribute and revoke one coworker's access to a CRM, mailbox or
calendar without touching another's (#129). Shared connector *code* is fine; a shared business-system login or
token across autonomous coworkers is not. This module is the credential-free contract for that: a registry of
provider authorizations, one per coworker per connection, and the resolution the governed boundary has to use. It
holds **references**, never secrets (``secret://`` names only), and it is **not wired into the sidecar**: today the
sidecar still resolves provider access from the environment-level binding.

- **One grant per coworker and connection.** A record binds an immutable tenant (client and environment), Agent,
  provider family, provider and connection id to an acting identity or isolated delegated grant, a secret
  *reference*, granted Tools, an owner, health, an optional expiry and a revocation state.
- **No shared credentials.** A secret reference or acting identity already held by another coworker is refused, and so
  is giving a second coworker a ``shared-login`` grant on a family where one coworker already has access. A
  ``shared-login`` grant is allowed for a lone coworker but is reported unsupported for autonomous multi-coworker use.
- **Attested caller only.** ``resolve`` takes the Agent name the runtime attested; there is no argument through which
  a model, a coordinator or a delegate can name another coworker or hand over a token. Rights are the originating
  principal's permissions ∩ the coworker's granted scopes ∩ the Tools enabled for it; a Tool outside that is refused.
- **Independent lifecycle.** Revoking, rotating or degrading one coworker's grant never changes another's; a revoked,
  expired or failed grant refuses resolution rather than falling back to anything shared.
- **Secure upgrade.** ``upgrade_from_environment`` turns an environment-level provider binding into a proposed grant
  only for a workspace with exactly one Agent; with several it refuses, so an old shared token is never silently reused.
- **Ledger.** Changes call the audit hook with identifiers, scopes and a digest of the secret reference, never the
  reference's name or any credential; ``receipt_fields`` gives the attribution a Tool call must carry.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
ISOLATIONS = ("own-identity", "isolated-grant", "shared-login")
SUPPORTED_ISOLATIONS = ("own-identity", "isolated-grant")
HEALTH = ("healthy", "degraded", "failed")
MAX_SCOPES = 50
MAX_GRANTS = 500
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FAMILY = re.compile(r"^[a-z]+$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CONNECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,191}$")
_SECRET_REF = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]{2,199}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_TOOL = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$")
Audit = Callable[[str, str, dict[str, Any]], Any]


class GrantError(PermissionError):
    """Raised for an invalid grant, an unsafe sharing of credentials or an access the grant does not allow."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _check(value: Any, pattern: re.Pattern[str], message: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise GrantError(message)
    return value


def _scopes(values: Iterable[Any]) -> list[str]:
    items = list(values)
    if not items or len(items) > MAX_SCOPES or not all(isinstance(item, str) and _TOOL.match(item) for item in items):
        raise GrantError(f"scopes are 1 to {MAX_SCOPES} portable Tool names such as crm.read")
    return sorted(set(items))


class GrantRegistry:
    def __init__(self, root: pathlib.Path, *, tenant: str, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise GrantError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.grants.json"
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        descriptor = os.open(self.root / f".{self.tenant}.grants.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "grants": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise GrantError("grant registry has an unsupported format or belongs to another tenant")
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

    def _record(self, action: str, by: str, grant: Mapping[str, Any], **extra: Any) -> None:
        if self._audit is not None:
            self._audit(action, by, {
                "grantId": grant["id"], "agent": grant["agent"], "family": grant["family"], "provider": grant["provider"],
                "connectionId": grant["connectionId"], "isolation": grant["isolation"], "scopes": list(grant["scopes"]),
                "secretRefDigest": digest(grant["secretRef"]), **extra,
            })

    @staticmethod
    def _public(grant: Mapping[str, Any]) -> dict[str, Any]:
        """A grant as shown to operators: the secret reference is shown only as a digest."""
        shown = {key: value for key, value in grant.items() if key != "secretRef"}
        shown["secretRefDigest"] = digest(grant["secretRef"])
        shown["multiCoworkerSupported"] = grant["isolation"] in SUPPORTED_ISOLATIONS
        return shown

    # -- provisioning -------------------------------------------------------------------

    def create(
        self,
        *,
        agent: str,
        family: str,
        provider: str,
        connection_id: str,
        isolation: str,
        acting_identity: str,
        secret_ref: str,
        scopes: Iterable[str],
        owner: str,
        by: str,
        expires_at: int | None = None,
        now: int,
    ) -> dict[str, Any]:
        """Record one coworker's own authorization; refuses anything that would share a credential or identity."""
        agent = _check(agent, _AGENT, "agent must be a short Agent name")
        family = _check(family, _FAMILY, "family must be lowercase letters such as crm")
        provider = _check(provider, _PROVIDER, "provider must be a short lowercase id such as hubspot")
        connection_id = _check(connection_id, _CONNECTION, "connection id must be a short identifier")
        if isolation not in ISOLATIONS:
            raise GrantError(f"isolation must be one of {', '.join(ISOLATIONS)}")
        acting_identity = _check(acting_identity, _IDENTITY, "acting identity must be the provider-side service identity or grant reference")
        secret_ref = _check(secret_ref, _SECRET_REF, "the credential must be a secret:// reference, never the value")
        owner = _check(owner, _PERSON, "owner must be a named person")
        who = _check(by, _PERSON, "by must name who provisioned it")
        granted = _scopes(scopes)
        if expires_at is not None and (not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at <= now):
            raise GrantError("expiry must be a future epoch-seconds time")
        with self._locked() as state:
            grants = state["grants"]
            if len(grants) >= MAX_GRANTS:
                raise GrantError("the grant registry is full")
            for other in grants.values():
                if other["agent"] == agent and other["connectionId"] == connection_id:
                    raise GrantError("this coworker already has a grant on that connection; rotate or revoke it instead")
                if other["agent"] != agent and other["secretRef"] == secret_ref:
                    raise GrantError("that secret reference already belongs to another coworker; each coworker needs its own credential")
                if other["agent"] != agent and other["provider"] == provider and other["actingIdentity"] == acting_identity:
                    raise GrantError("that provider identity already belongs to another coworker; each coworker needs its own identity or isolated grant")
                if other["agent"] != agent and other["family"] == family and "shared-login" in (other["isolation"], isolation) and not other.get("revokedAt"):
                    raise GrantError("a shared-login grant is unsupported for several coworkers on one family; use separate identities or isolated grants")
            grant = {
                "id": "g-" + secrets.token_hex(6), "tenant": self.tenant, "agent": agent, "family": family, "provider": provider,
                "connectionId": connection_id, "isolation": isolation, "actingIdentity": acting_identity, "secretRef": secret_ref,
                "scopes": granted, "owner": owner, "health": "healthy", "createdAt": now, "createdBy": who, "rotations": 0,
                **({"expiresAt": expires_at} if expires_at is not None else {}),
            }
            grants[grant["id"]] = grant
        self._record("grant.create", who, grant)
        return self._public(grant)

    def _change(self, grant_id: str, by: str, action: str, apply: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        who = _check(by, _PERSON, "by must name who made the change")
        with self._locked() as state:
            grant = state["grants"].get(grant_id)
            if grant is None:
                raise GrantError("no such grant")
            extra = apply(grant, state["grants"])
            snapshot = dict(grant)
        self._record(action, who, snapshot, **extra)
        return self._public(snapshot)

    def revoke(self, grant_id: str, *, by: str, now: int) -> dict[str, Any]:
        """Withdraw one coworker's access; every other coworker's grant is untouched."""
        def apply(grant: dict[str, Any], _: dict[str, Any]) -> dict[str, Any]:
            grant["revokedAt"] = grant.get("revokedAt", now)
            grant["health"] = "failed"
            return {}
        return self._change(grant_id, by, "grant.revoke", apply)

    def revoke_all(self, *, by: str, now: int, agent: str | None = None) -> list[dict[str, Any]]:
        """Offboard: revoke every live grant of one coworker (``agent``) or of the whole tenant (``agent=None``).

        Each revoked grant is ledgered like a single revoke (secret reference as a digest only). The result lists the
        revoked grants as operators see them, plus ``secretRef`` so the person offboarding knows which entries to delete from
        the secret store: revoking here only stops the coworker using the grant, it does not delete the credential or revoke
        it at the provider. Already-revoked grants are left as they are, so repeating the call changes nothing.
        """
        who = _check(by, _PERSON, "by must name who offboarded it")
        if agent is not None:
            agent = _check(agent, _AGENT, "agent must be a short Agent name")
        revoked: list[dict[str, Any]] = []
        with self._locked() as state:
            for grant in state["grants"].values():
                if (agent is None or grant["agent"] == agent) and not grant.get("revokedAt"):
                    grant["revokedAt"] = now
                    grant["health"] = "failed"
                    revoked.append(dict(grant))
        for grant in sorted(revoked, key=lambda item: (item["createdAt"], item["id"])):
            self._record("grant.revoke", who, grant, offboarded=True)
        return [{**self._public(grant), "secretRef": grant["secretRef"]} for grant in sorted(revoked, key=lambda item: (item["createdAt"], item["id"]))]

    def rotate(self, grant_id: str, *, secret_ref: str, by: str, now: int) -> dict[str, Any]:
        """Replace this coworker's credential reference with a new one nobody else holds."""
        secret_ref = _check(secret_ref, _SECRET_REF, "the credential must be a secret:// reference, never the value")

        def apply(grant: dict[str, Any], grants: dict[str, Any]) -> dict[str, Any]:
            if grant.get("revokedAt"):
                raise GrantError("a revoked grant cannot be rotated; provision a new one")
            if any(other["secretRef"] == secret_ref for other in grants.values()):
                raise GrantError("that secret reference is already in use; a rotated credential must be new")
            grant.update(secretRef=secret_ref, rotatedAt=now, rotations=grant["rotations"] + 1, health="healthy")
            return {}
        return self._change(grant_id, by, "grant.rotate", apply)

    def set_health(self, grant_id: str, health: str, *, by: str) -> dict[str, Any]:
        if health not in HEALTH:
            raise GrantError(f"health must be one of {', '.join(HEALTH)}")

        def apply(grant: dict[str, Any], _: dict[str, Any]) -> dict[str, Any]:
            if grant.get("revokedAt"):
                raise GrantError("a revoked grant stays failed")
            grant["health"] = health
            return {"health": health}
        return self._change(grant_id, by, "grant.health", apply)

    # -- reads --------------------------------------------------------------------------

    def list(self, *, agent: str | None = None) -> list[dict[str, Any]]:
        with self._locked() as state:
            grants = [self._public(item) for item in state["grants"].values()]
        if agent is not None:
            grants = [item for item in grants if item["agent"] == agent]
        return sorted(grants, key=lambda item: (item["createdAt"], item["id"]))

    def resolve(
        self,
        *,
        attested_agent: str,
        family: str,
        tool: str,
        originator_permissions: Iterable[str],
        enabled_tools: Iterable[str],
        now: int,
    ) -> dict[str, Any]:
        """The grant an attested coworker may use for ``tool``, or ``GrantError``.

        ``attested_agent`` must come from the runtime's caller context, never from model or tool arguments. The
        result carries the secret *reference* the connector should resolve for this coworker alone.
        """
        agent = _check(attested_agent, _AGENT, "the attested Agent name is required")
        with self._locked() as state:
            candidates = [dict(item) for item in state["grants"].values() if item["agent"] == agent and item["family"] == family]
        if not candidates:
            raise GrantError(f"{agent} has no {family} grant of its own")
        usable = []
        refusal = "no usable grant"
        for grant in sorted(candidates, key=lambda item: item["createdAt"]):
            if grant.get("revokedAt"):
                refusal = "its grant is revoked"
            elif grant.get("expiresAt") is not None and grant["expiresAt"] <= now:
                refusal = "its grant has expired"
            elif grant["health"] == "failed":
                refusal = "its grant has failed"
            else:
                usable.append(grant)
        if not usable:
            raise GrantError(f"{agent} cannot use {family}: {refusal}")
        permitted, enabled = set(originator_permissions), set(enabled_tools)
        for grant in usable:
            if tool in permitted and tool in grant["scopes"] and tool in enabled:
                return {
                    "grantId": grant["id"], "agent": agent, "connectionId": grant["connectionId"], "provider": grant["provider"],
                    "actingIdentity": grant["actingIdentity"], "secretRef": grant["secretRef"], "scopes": list(grant["scopes"]),
                    "isolation": grant["isolation"], "degraded": grant["health"] == "degraded",
                }
        raise GrantError(
            f"{tool} is outside what the requester, {agent}'s grant and its enabled Tools all allow"
        )


def receipt_fields(access: Mapping[str, Any], *, initiating_principal: str, request_id: str) -> dict[str, Any]:
    """Attribution a Tool call's ledger entry must carry: who acted, for whom, under which provider-side identity."""
    return {
        "agent": access["agent"], "initiatingPrincipal": initiating_principal, "grantId": access["grantId"],
        "connectionId": access["connectionId"], "provider": access["provider"], "actingIdentity": access["actingIdentity"],
        "scopes": list(access["scopes"]), "requestId": request_id,
    }


def upgrade_from_environment(binding: Mapping[str, Any], agents: Iterable[str]) -> list[dict[str, Any]]:
    """Proposed grants for a single-Agent workspace from its environment-level provider bindings.

    A workspace with several Agents is refused: an environment token must never become several coworkers' shared
    credential. The proposals are marked ``shared-login`` because that is what an environment token is; an operator
    replaces them with own-identity or isolated grants before adding a second coworker.
    """
    names = list(agents)
    if len(names) != 1:
        raise GrantError(
            "refusing to reuse environment-level provider tokens for several coworkers; provision a separate identity or "
            "isolated grant for each coworker (#129, #110)"
        )
    proposals = []
    for family, entry in sorted((binding.get("connectors") or {}).items()):
        if not isinstance(entry, Mapping) or int(entry.get("tier", 1)) == 0:
            continue
        reference = entry.get("token_ref") or entry.get("secret_ref")
        if not isinstance(reference, str) or not _SECRET_REF.match(reference):
            continue
        proposals.append({
            "agent": names[0], "family": family, "provider": entry.get("provider"), "isolation": "shared-login",
            "secretRef": reference, "multiCoworkerSupported": False,
        })
    return proposals
