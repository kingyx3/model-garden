#!/usr/bin/env python3
"""Show or set live connection health, and offboard a client environment (run inside governed-tools).

  runtime-connection.py /opt/data/.modelgarden list
  runtime-connection.py /opt/data/.modelgarden set calendar revoked --actor jordan@client.example --reason "token rotated"
  runtime-connection.py /opt/data/.modelgarden offboard --actor jordan@client.example --reason "contract ended"
  runtime-connection.py /opt/data/.modelgarden offboard --agent receptionist --actor jordan@client.example
  runtime-connection.py /opt/data/.modelgarden grants list [--agent receptionist]
  runtime-connection.py /opt/data/.modelgarden grants revoke g-0123456789ab --actor jordan@client.example

Marking a connection ``revoked`` or ``failed`` takes effect on the coworker's next
``platform.capabilities`` read and is enforced at the governed boundary, without redeploying Agents.
Every change writes a chained ``operator-action`` ledger event naming the acting principal, the
capability family and the new health (never a credential, scope or account).

``offboard`` revokes every bound external (tier 1 and above) connection listed in the capability manifest
and, when a per-coworker grant registry exists (``provider-grants/<client>-<environment>.grants.json``, #129),
every grant in it; each revoked grant is ledgered. With ``--agent`` it offboards one coworker only: that
coworker's grants are revoked and every other coworker keeps working (the environment-level connection health
is left alone). It prints the steps only a person can perform: the platform cannot revoke a grant at the
provider, delete secrets from the secret store (the output lists the secret *references* of the revoked grants
so each can be deleted), erase local data or tear the stack down.

Operator-only ``grants create`` provisions a separately scoped coworker grant; ``grants rotate``
rebinds it to a fresh secret reference and ``grants health`` marks failure or
recovery. These commands accept ONLY ``secret://`` *references*, never secret values,
and they do not install, resolve or authorize a provider credential. The external
secret must already be provisioned in the approved tenant vault. Every change is
ledgered; ``grants list`` and create/rotate/health outputs are redacted by the store.
After revocation a new grant for the same coworker/connection requires a NEW
secret reference: the revoked one is never silently reused.

``credential open|status|revoke`` manage owner-entered credentials (#159, #86). ``open`` starts a single-use,
expiring enrollment for one coworker's connection and prints the link to send to the named owner; only that
owner, signed in to the gateway, can submit on it, and the value goes straight to the sealed vault (never to
this command, the ledger or chat). It also prints the stable ``secret://`` reference to put in the grant and
the connection declaration; rotating later is another ``open`` and needs no restaging. ``revoke`` deletes the
credential from the vault.

  runtime-connection.py /opt/data/.modelgarden credential open --agent receptionist --connection orders-api \
      --kind bearer --owner owner@client.example --actor jordan@client.example --origin https://chat.client.example

``oauth open|status|revoke`` manage provider OAuth consent (#66, #86, #110). ``open`` starts a single-use consent request for one
coworker's connection, the named owner and the approved Tools; the owner signs in, reviews the scopes and consents at the provider
(Google, Microsoft or a declared OIDC/OAuth provider). Tokens go only to the sealed vault; the consent is recorded as attested by the
owner's own session. Run ``revoke`` (and ``offboard``) where the vault private key is injected (the governed sidecar) to also revoke the
refresh token at the provider; without it the tokens are only deleted locally and the output says so.

  runtime-connection.py /opt/data/.modelgarden oauth open --agent receptionist --connection calendar --provider google \
      --tool calendar.read --owner owner@client.example --actor jordan@client.example --origin https://chat.client.example
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

OFFBOARDING_CHECKLIST = (
    "Revoke each provider authorization at the provider (the Google or Microsoft admin consent, API keys, OAuth grants): "
    "revoking here only stops the coworker using them.",
    "Delete this environment's secrets from the secret store (the GitHub Environment secrets or the cloud secret manager).",
    "Export and verify the action ledger before anything is deleted: scripts/ledger-export.py export, then verify.",
    "Decide what happens to local data under the client's retention rules: the contact log (client-operator.py contacts), "
    "call transcripts and fallback messages, hand-offs and approvals; erase what must not be kept.",
    "Stop and remove the stack and its volumes (docker compose down -v) and, for a cloud project, run the Terraform destroy.",
)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CAPABILITIES = _load("model_garden_connection_capabilities", ROOT / "platform" / "core" / "capabilities.py")
GOVERN = _load("model_garden_connection_govern", ROOT / "scripts" / "govern-action.py")
GRANTS = _load("model_garden_connection_grants", ROOT / "platform" / "core" / "provider_grants.py")
READINESS = _load("model_garden_connection_grant_readiness", ROOT / "platform" / "core" / "grant_readiness.py")
ENROLLMENT = _load("model_garden_connection_enrollment", ROOT / "platform" / "core" / "secret_enrollment.py")
VAULT = _load("model_garden_connection_vault", ROOT / "platform" / "core" / "sealed_vault.py")
OAUTH = _load("model_garden_connection_oauth", ROOT / "platform" / "core" / "oauth_consent.py")


def external_families(manifest: dict) -> list[str]:
    """Families the manifest shows bound to an external (tier 1+) provider."""
    return sorted(
        entry["family"] for entry in manifest.get("capabilities", [])
        if isinstance(entry, dict) and entry.get("status") in ("available", "unavailable", "not-granted")
        and isinstance(entry.get("tier"), int) and entry["tier"] >= 1 and isinstance(entry.get("family"), str)
    )


def _manifest(args: argparse.Namespace, *, required: bool) -> dict:
    path = args.manifest or (
        pathlib.Path(os.environ["MODEL_GARDEN_CAPABILITY_MANIFEST"]) if os.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST") else None
    )
    if path is None or not path.is_file():
        if required:
            raise ValueError("the capability manifest is needed to know which connections are bound; pass --manifest")
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _registry(args: argparse.Namespace, manifest: dict, audit_path: pathlib.Path, *, required: bool, create: bool = False):
    """The environment's grant registry, or ``None`` when none exists (single-Agent deployments before grants).

    ``required`` is set when the caller asked about specific grants or one coworker: an unknown environment is then an
    error rather than "no grants".
    """
    stated = READINESS.tenant_of(manifest)
    if args.tenant and stated and args.tenant != stated:
        raise ValueError("the supplied tenant does not match the environment manifest")
    tenant = args.tenant or stated
    if tenant is None:
        if required:
            raise ValueError("cannot tell which environment's grants to use; pass --tenant <client>-<environment> or --manifest")
        return None
    root = args.store / READINESS.REGISTRY_DIR
    target = root / f"{tenant}.grants.json"
    if target.is_symlink() or root.is_symlink():
        raise ValueError("grant registry cannot be a symbolic link")
    if not target.is_file() and not create:
        return None
    return GRANTS.GrantRegistry(
        root, tenant=tenant, audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details)
    )


def _oauth_store(args: argparse.Namespace, manifest_path: pathlib.Path | None, audit_path: pathlib.Path, *, required: bool = True):
    """The OAuth consent store for this environment, or ``None`` when OAuth is not configured and ``required`` is false."""
    environ = dict(os.environ)
    config = args.gateway_config or environ.get("MODEL_GARDEN_OAUTH_CONFIG") or environ.get("MODEL_GARDEN_GATEWAY_CONFIG") or ""
    if not config or not pathlib.Path(config).is_file():
        if required:
            raise ValueError("pass --gateway-config (the gateway's rendered gateway.json, which declares gateway.oauth)")
        return None
    environ["MODEL_GARDEN_OAUTH_CONFIG"] = str(config)
    environ["MODEL_GARDEN_OAUTH_ROOT"] = str(args.store)
    if manifest_path is not None:
        environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(manifest_path)
    vault_root = pathlib.Path(environ.get("MODEL_GARDEN_VAULT_ROOT") or args.store / "vault")
    store = OAUTH.store_from_environment(environ, vault=VAULT.SealedVault(vault_root), audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details))
    if store is None:
        if required:
            raise ValueError("the gateway configuration declares no gateway.oauth")
        return None
    private = environ.get("MODEL_GARDEN_VAULT_PRIVATE_KEY", "").strip()
    read = VAULT.SealedResolver(vault_root, private_key=private) if private else None
    return store, read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="governance data root, e.g. /opt/data/.modelgarden")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show recorded connection health")
    mark = sub.add_parser("set", help="record one connection's health")
    mark.add_argument("family", help="capability family, e.g. calendar")
    mark.add_argument("health", choices=CAPABILITIES.CONNECTION_HEALTH)
    mark.add_argument("--actor", required=True, help="operator identity recording the change")
    mark.add_argument("--reason", help="short non-secret reason")
    off = sub.add_parser("offboard", help="revoke every bound external connection and grant (or one coworker's) and print the manual steps")
    off.add_argument("--actor", required=True, help="the named person offboarding")
    off.add_argument("--reason", help="short non-secret reason")
    off.add_argument("--agent", help="offboard this coworker only: revoke its grants and leave every other coworker working")
    grants = sub.add_parser("grants", help="show or revoke per-coworker provider grants")
    grant_sub = grants.add_subparsers(dest="grants_command", required=True)
    grant_list = grant_sub.add_parser("list", help="show grants (no secret references)")
    grant_list.add_argument("--agent", help="only this coworker's grants")
    grant_revoke = grant_sub.add_parser("revoke", help="revoke one grant; no other coworker is affected")
    grant_revoke.add_argument("grant_id")
    grant_revoke.add_argument("--actor", required=True)
    grant_create = grant_sub.add_parser("create", help="provision one coworker's independently scoped grant (references only)")
    grant_create.add_argument("--agent", required=True)
    grant_create.add_argument("--family", required=True)
    grant_create.add_argument("--provider", required=True)
    grant_create.add_argument("--connection-id", required=True)
    grant_create.add_argument("--isolation", choices=GRANTS.SUPPORTED_ISOLATIONS, required=True)
    grant_create.add_argument("--acting-identity", required=True, help="provider-side coworker identity, never a token")
    grant_create.add_argument("--secret-ref", required=True, help="secret:// vault reference, never its value")
    grant_create.add_argument("--scope", action="append", required=True, help="portable Tool; repeat for every allowed operation")
    grant_create.add_argument("--owner", required=True, help="person responsible for this grant")
    grant_create.add_argument("--actor", required=True, help="operator creating the grant")
    grant_create.add_argument("--expires-at", type=int, help="optional expiry as Unix seconds")
    grant_rotate = grant_sub.add_parser("rotate", help="replace one coworker's existing secret reference")
    grant_rotate.add_argument("grant_id")
    grant_rotate.add_argument("--secret-ref", required=True, help="fresh secret:// reference; no token material")
    grant_rotate.add_argument("--actor", required=True)
    grant_health = grant_sub.add_parser("health", help="record provider grant health for one coworker")
    grant_health.add_argument("grant_id")
    grant_health.add_argument("health", choices=GRANTS.HEALTH)
    grant_health.add_argument("--actor", required=True)
    credential = sub.add_parser("credential", help="owner-entered credentials: open an enrollment link, show status, revoke")
    credential_sub = credential.add_subparsers(dest="credential_command", required=True)
    credential_open = credential_sub.add_parser("open", help="start a single-use enrollment and print the owner's link")
    credential_open.add_argument("--kind", choices=ENROLLMENT.KINDS, required=True)
    credential_open.add_argument("--owner", required=True, help="the client owner who will enter the value (they must sign in as this address)")
    credential_open.add_argument("--origin", required=True, help="the gateway's public https origin")
    credential_open.add_argument("--minutes", type=int, default=15, help="how long the link works (1 to 60)")
    credential_status = credential_sub.add_parser("status", help="whether a credential is set (masked, never the value)")
    credential_revoke = credential_sub.add_parser("revoke", help="delete the credential from the vault")
    for each in (credential_open, credential_status, credential_revoke):
        each.add_argument("--agent", required=True)
        each.add_argument("--connection", required=True)
    for each in (credential_open, credential_revoke):
        each.add_argument("--actor", required=True, help="the named person doing this")
    oauth = sub.add_parser("oauth", help="provider OAuth consent: open a consent link for the owner, show status, revoke")
    oauth_sub = oauth.add_subparsers(dest="oauth_command", required=True)
    oauth_open = oauth_sub.add_parser("open", help="start a single-use consent request and print the owner's link")
    oauth_open.add_argument("--provider", required=True, help="a provider declared in gateway.oauth, such as google")
    oauth_open.add_argument("--tool", action="append", required=True, help="an approved Tool this consent is for; repeat. Scopes are only what these need")
    oauth_open.add_argument("--owner", required=True, help="the client owner who must consent (they must sign in as this address)")
    oauth_open.add_argument("--origin", required=True, help="the gateway's public https origin")
    oauth_open.add_argument("--minutes", type=int, default=15, help="how long the link works (1 to 60)")
    oauth_status = oauth_sub.add_parser("status", help="whether consent is held (no token, reference or scope secrets)")
    oauth_revoke = oauth_sub.add_parser("revoke", help="revoke at the provider (if the vault key is here) and delete the tokens")
    for each in (oauth_open, oauth_status, oauth_revoke):
        each.add_argument("--agent", required=True)
        each.add_argument("--connection", required=True)
    for each in (oauth_open, oauth_revoke):
        each.add_argument("--actor", required=True, help="the named person doing this")
    for each in (oauth_open, oauth_status, oauth_revoke, off):
        each.add_argument("--gateway-config", type=pathlib.Path, help="gateway.json declaring gateway.oauth (default: MODEL_GARDEN_OAUTH_CONFIG or MODEL_GARDEN_GATEWAY_CONFIG)")
    for each in (off, grant_list, grant_revoke, grant_create, grant_rotate, grant_health, credential_open, credential_status, credential_revoke,
                 oauth_open, oauth_status, oauth_revoke):
        each.add_argument("--manifest", type=pathlib.Path, help="capability manifest (default: MODEL_GARDEN_CAPABILITY_MANIFEST)")
        each.add_argument("--tenant", help="grant registry tenant <client>-<environment> (default: from the manifest)")
    args = parser.parse_args(argv)
    audit_path = args.audit_path or args.store / "audit.jsonl"
    try:
        if args.command == "list":
            print(json.dumps(CAPABILITIES.read_connection_health(args.store), indent=2, sort_keys=True))
            return 0
        if args.command == "set":
            record = CAPABILITIES.record_connection_health(
                args.store, args.family, args.health, actor=args.actor, reason=args.reason
            )
            details = {"family": args.family, "health": args.health}
            if args.reason:
                details["reason"] = args.reason[:200]
            GOVERN.record_operator_action(audit_path, "connection.set", args.actor, details)
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0
        if args.command == "credential":
            manifest = _manifest(args, required=False)
            tenant = args.tenant or READINESS.tenant_of(manifest)
            if not tenant:
                raise ValueError("cannot tell which environment this is for; pass --tenant <client>-<environment> or --manifest")
            store = ENROLLMENT.EnrollmentStore(
                args.store / "secret-enrollments", tenant=tenant, vault=VAULT.SealedVault(args.store / "vault"),
                audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
            )
            now = int(time.time())
            if args.credential_command == "open":
                if not args.origin.startswith("https://") or args.origin.count("/") != 2:
                    raise ValueError("--origin must be the gateway's https origin, such as https://chat.client.example")
                if not 1 <= args.minutes <= 60:
                    raise ValueError("--minutes must be 1 to 60")
                opened = store.create(agent=args.agent, connection_id=args.connection, kind=args.kind, owner=args.owner,
                                      requested_by=args.actor, now=now, ttl_seconds=args.minutes * 60)
                print(json.dumps({
                    "enrollmentId": opened["enrollmentId"], "expiresAt": opened["expiresAt"], "owner": args.owner,
                    "link": f"{args.origin}/connections/enroll#enroll={opened['token']}",
                    "secretRef": f"secret://{tenant}/{args.agent}/{args.connection}",
                    "next": "Send the link to the owner only. Use secretRef in the coworker's grant and the connection declaration.",
                }, indent=2, sort_keys=True))
                return 0
            if args.credential_command == "status":
                state = store.status(args.agent, args.connection, now=now)
                state.pop("ref", None)
                print(json.dumps(state, indent=2, sort_keys=True))
                return 0
            print(json.dumps(store.revoke(args.agent, args.connection, by=args.actor, now=now), indent=2, sort_keys=True))
            return 0
        if args.command == "oauth":
            manifest_text = args.manifest or (pathlib.Path(os.environ["MODEL_GARDEN_CAPABILITY_MANIFEST"]) if os.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST") else None)
            store, read = _oauth_store(args, manifest_text, audit_path)
            now = int(time.time())
            if args.oauth_command == "open":
                if not args.origin.startswith("https://") or args.origin.count("/") != 2:
                    raise ValueError("--origin must be the gateway's https origin, such as https://chat.client.example")
                if not 1 <= args.minutes <= 60:
                    raise ValueError("--minutes must be 1 to 60")
                opened = store.request(agent=args.agent, connection_id=args.connection, provider=args.provider, tools=args.tool, owner=args.owner,
                                       requested_by=args.actor, now=now, ttl_seconds=args.minutes * 60)
                print(json.dumps({
                    "requestId": opened["requestId"], "expiresAt": opened["expiresAt"], "owner": args.owner, "scopes": opened["scopes"],
                    "link": f"{args.origin}/connections/oauth/start#consent={opened['token']}", "secretRef": opened["secretRef"],
                    "next": "Send the link to the owner only. Use secretRef in the coworker's grant and the connection declaration.",
                }, indent=2, sort_keys=True))
                return 0
            if args.oauth_command == "status":
                print(json.dumps(store.status(args.agent, args.connection, now=now), indent=2, sort_keys=True))
                return 0
            print(json.dumps(store.revoke(args.agent, args.connection, by=args.actor, now=now, read=read), indent=2, sort_keys=True))
            return 0
        if args.command == "grants":
            manifest = _manifest(args, required=False)
            registry = _registry(args, manifest, audit_path, required=True, create=args.grants_command == "create")
            if registry is None:
                if args.grants_command != "list":
                    raise ValueError("no grant registry exists for this tenant; create a grant first")
                print(json.dumps({"grants": [], "registry": "none"}, indent=2, sort_keys=True))
                return 0
            if args.grants_command == "list":
                print(json.dumps({"grants": registry.list(agent=args.agent)}, indent=2, sort_keys=True))
                return 0
            if args.grants_command == "create":
                # A grant can only authorize known portable Tools of the named family.
                # Unknown or cross-family scopes cannot accidentally acquire authority later.
                scopes = sorted(set(args.scope))
                if any(CAPABILITIES.catalog_tool(tool) is None or tool.split(".", 1)[0] != args.family for tool in scopes):
                    raise ValueError("grant scopes must be catalogued portable Tools of the named family")
                created = registry.create(
                    agent=args.agent, family=args.family, provider=args.provider,
                    connection_id=args.connection_id, isolation=args.isolation,
                    acting_identity=args.acting_identity, secret_ref=args.secret_ref,
                    scopes=scopes, owner=args.owner, by=args.actor,
                    expires_at=args.expires_at, now=int(time.time()),
                )
                print(json.dumps({"created": created}, indent=2, sort_keys=True))
                return 0
            if args.grants_command == "rotate":
                rotated = registry.rotate(args.grant_id, secret_ref=args.secret_ref, by=args.actor, now=int(time.time()))
                print(json.dumps({"rotated": rotated}, indent=2, sort_keys=True))
                return 0
            if args.grants_command == "health":
                state = registry.set_health(args.grant_id, args.health, by=args.actor)
                print(json.dumps({"updated": state}, indent=2, sort_keys=True))
                return 0
            revoked = registry.revoke(args.grant_id, by=args.actor, now=int(time.time()))
            print(json.dumps({"revoked": {key: revoked[key] for key in ("id", "agent", "family", "provider")}}, indent=2, sort_keys=True))
            return 0
        manifest = _manifest(args, required=args.agent is None)
        agent = args.agent
        families: list[str] = []
        if agent is None:
            families = external_families(manifest)
            for family in families:
                CAPABILITIES.record_connection_health(
                    args.store, family, "revoked", actor=args.actor, reason=(args.reason or "environment offboarded")
                )
        registry = _registry(args, manifest, audit_path, required=agent is not None)
        revoked_grants = registry.revoke_all(by=args.actor, now=int(time.time()), agent=agent) if registry is not None else []
        oauth_revoked: list = []
        if (args.store / "oauth-consents").is_dir():
            try:
                oauth_loaded = _oauth_store(args, args.manifest, audit_path, required=False)
                if oauth_loaded is not None:
                    oauth_revoked = oauth_loaded[0].revoke_all(by=args.actor, now=int(time.time()), agent=agent, read=oauth_loaded[1])
            except (OAUTH.OAuthError, ValueError, OSError) as exc:
                oauth_revoked = [{"state": "error", "error": str(exc)}]
        details: dict = {"families": families}
        if registry is not None:
            details["grantsRevoked"] = len(revoked_grants)
        if agent is not None:
            details["agent"] = agent
        if args.reason:
            details["reason"] = args.reason[:200]
        GOVERN.record_operator_action(audit_path, "coworker.offboard" if agent else "environment.offboard", args.actor, details)
        print(json.dumps({
            "revoked": families,
            "grantsRevoked": [{key: item[key] for key in ("id", "agent", "family", "provider")} for item in revoked_grants],
            "secretRefsToDelete": sorted({item["secretRef"] for item in revoked_grants}),
            **({"oauthConsentsRevoked": oauth_revoked} if oauth_revoked else {}),
            "manualSteps": list(OFFBOARDING_CHECKLIST),
        }, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, ENROLLMENT.EnrollmentError, VAULT.VaultError, OAUTH.OAuthError) as exc:
        print(f"Runtime connection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
