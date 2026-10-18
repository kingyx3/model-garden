"""Provider OAuth 2.0 consent captured from the named owner, with tokens kept only in the sealed vault (#66, #86, #110, #129).

Owner-entered credentials (``secret_enrollment``) cover API keys. A client's Google or Microsoft account, or any OIDC/OAuth
provider, instead needs the owner to *consent at the provider*. This is the credential-free core of that authorization-code +
PKCE flow. It makes no network call of its own (an injected ``http`` does, and the default one is endpoint-guarded), holds no
token except by handing it straight to the vault, and is driven by the gateway (``/connections/oauth/start`` and
``/connections/oauth/callback``), the governed sidecar (``RefreshingResolver``) and ``scripts/runtime-connection.py oauth``.

- **Declared, pinned providers.** ``load_providers`` validates the deployment's ``gateway.oauth.providers``: a preset (``google``,
  ``microsoft``) or a declared generic provider with an exact-host allow-list. The authorize, token and revoke endpoints all go
  through ``endpoint_guard.parse_endpoint`` (https, public host, no query, port 443) and must be on that allow-list, and the
  authorize URL is built from the pinned configuration only: nothing in a request chooses a host, so there is no open redirect, and
  the token call is made to a pinned, public, resolved address, so there is no SSRF. The redirect URI is the gateway's own.
- **Request, consent page, redirect.** An operator opens a *consent request* for one coworker's connection, the named owner and a
  list of approved Tools. The scopes asked of the provider are exactly the union of the OAuth scopes declared for those Tools
  (a Tool with no declared scope is refused), plus the scopes the provider needs for refresh (``offline_access``). The owner opens
  the link, signs in with the gateway's web sign-in, and sees the provider, the Tools and every scope before approving.
- **State and PKCE bound and single use.** Approving mints a random ``state`` and a PKCE verifier (S256), stored only as a digest
  (state) and server-side (verifier) for ten minutes and bound to the request, owner, coworker and connection. The callback accepts a
  state once: replay, an unknown or mismatched state, an expired one, and a different signed-in person are all refused and
  ledgered without the code or any token. The callback is completed by the owner's verified session, not by the redirect alone.
- **Scopes cannot widen.** The token response's granted scopes must equal what was asked (nothing extra, nothing missing);
  otherwise nothing is stored and the grant is revoked at the provider, best effort. The same check applies on every refresh.
- **Tokens only in the sealed vault.** The access token and the refresh token are ``put`` to the vault under versioned references
  and dropped. The connection record, the ledger, notices and every return value carry no token, no digest of one, and no
  reference name (references appear only as stable names in operator output). The stable access reference
  (``secret://<tenant>/<agent>/<connection>.oauth``) is what the grant and the connection declaration name, so the sidecar's
  existing resolver returns the current access token as a bearer credential.
- **Owner-attested consent.** On success the ledger records ``oauth.consent-granted`` *by the owner's verified session* with
  ``attestation: owner-session``, the provider, scopes, Tools and fingerprints of the client id and the granted scopes. An operator
  can request a consent but can never record one.
- **Refresh on use, revocation noticed.** ``RefreshingResolver`` (installed in the governed sidecar, the only holder of the vault's
  private key) calls ``ensure_fresh`` before returning an access token that is about to expire. A rotated refresh token is written
  before the old one is deleted. A provider answer of ``invalid_grant`` means the owner or admin revoked access: the vault entries are
  deleted, the connection becomes ``revoked-at-provider`` and the coworker's grant health becomes ``failed``, which the existing
  notices (``connection_notices``) deliver to the owner. A transient failure marks the grant ``degraded`` and fails only that call.
- **Offboarding.** ``revoke`` revokes the refresh token at the provider when the provider declares a revocation endpoint and the
  caller can open the vault, then deletes both tokens from the vault. The result says whether the provider revoked it.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import pathlib
import re
import secrets
import socket
import ssl
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
REQUEST_TTL = 15 * 60
MAX_REQUEST_TTL = 60 * 60
STATE_TTL = 10 * 60
MAX_STARTS = 5
MAX_STATES = 200
MAX_LIVE_REQUESTS = 200
REFRESH_SKEW = 120
DEFAULT_ACCESS_TTL = 3600
MAX_RESPONSE = 65536
HTTP_TIMEOUT = 15
REVOKED_AT_PROVIDER = ("invalid_grant", "invalid_token", "access_denied")
RETRYABLE = ("invalid_client", "temporarily_unavailable", "server_error")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CONNECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{2,255}$")
_TOOL = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_CODE = re.compile(r"^[\x21-\x7e]{1,2048}$")
_ERROR = re.compile(r"^[a-z_]{1,40}$")
_PARAM_VALUE = re.compile(r"^[A-Za-z0-9._~:/ -]{1,128}$")
AUTH_PARAMS = ("access_type", "prompt", "include_granted_scopes", "audience", "resource", "hd", "domain_hint")
Audit = Callable[[str, str, dict[str, Any]], Any]
Http = Callable[[str, str, Mapping[str, str], bytes], "tuple[int, bytes]"]

PRESETS: dict[str, dict[str, Any]] = {
    "google": {
        "authorizeUrl": "https://accounts.google.com/o/oauth2/v2/auth",
        "tokenUrl":
            "https://oauth2.googleapis.com/token",
        "revokeUrl": "https://oauth2.googleapis.com/revoke", "allowedHosts": ["accounts.google.com", "oauth2.googleapis.com"],
        "authParams": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"},
        "implicitScopes": [], "benignScopes": ["openid", "email", "profile"], "scopePrefixes": [],
    },
    "microsoft": {
        "authorizeUrl": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "tokenUrl":
            "https://login.microsoftonline.com/common/oauth2/v2.0/token", "revokeUrl": None,
        "allowedHosts": ["login.microsoftonline.com"], "authParams": {"prompt": "consent"},
        "implicitScopes": ["offline_access"], "benignScopes": ["openid", "email", "profile"],
        "scopePrefixes": ["https://graph.microsoft.com/"],
    },
}
_PROVIDER_KEYS = {"preset", "clientId", "authorizeUrl", "tokenUrl", "revokeUrl", "allowedHosts", "scopesByTool", "authParams",
                  "implicitScopes", "benignScopes", "scopePrefixes"}


class OAuthError(PermissionError):
    """A consent, refresh or revocation is not allowed or failed; the message names no token, code, state or reference."""


def _sibling(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _guard():
    return _sibling("model_garden_oauth_endpoint_guard", "endpoint_guard.py")


def client_secret_env(provider: str) -> str:
    """The environment variable that carries a provider's OAuth client secret into the gateway and the governed sidecar."""
    return "MODEL_GARDEN_OAUTH_" + provider.upper().replace("-", "_") + "_CLIENT_SECRET"


def tenant_of_manifest(manifest: Any) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    client, environment = manifest.get("client"), manifest.get("environment")
    if not isinstance(client, str) or not isinstance(environment, str):
        return None
    slug = f"{client}-{environment}".lower()
    return slug if _TENANT.match(slug) else None


# -- provider declarations ------------------------------------------------------------------------------------------------

def _scope_list(values: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(values, list) or (not values and not allow_empty) or len(values) > 20 or not all(isinstance(v, str) and _SCOPE.match(v) for v in values):
        raise OAuthError(f"{label} must be a short list of OAuth scope names")
    return sorted(set(values))


def load_providers(declared: Any) -> dict[str, dict[str, Any]]:
    """Validate ``gateway.oauth.providers`` and return each provider with pinned, checked endpoints."""
    if not isinstance(declared, Mapping) or not declared or len(declared) > 8:
        raise OAuthError("oauth.providers names between 1 and 8 providers")
    guard = _guard()
    providers: dict[str, dict[str, Any]] = {}
    for name, raw in declared.items():
        if not isinstance(name, str) or not _PROVIDER_NAME.match(name):
            raise OAuthError("a provider name is a short lowercase identifier such as google")
        if not isinstance(raw, Mapping) or set(raw) - _PROVIDER_KEYS:
            raise OAuthError(f"oauth provider {name} holds only: {', '.join(sorted(_PROVIDER_KEYS))}")
        client_id = raw.get("clientId")
        if not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
            raise OAuthError(f"oauth provider {name} needs a clientId")
        base: dict[str, Any]
        if raw.get("preset") is not None:
            if raw["preset"] not in PRESETS:
                raise OAuthError(f"oauth provider {name}: preset must be one of {', '.join(sorted(PRESETS))}")
            if {"authorizeUrl", "tokenUrl", "revokeUrl", "allowedHosts"} & set(raw):
                raise OAuthError(f"oauth provider {name}: a preset's endpoints and hosts are pinned and cannot be overridden")
            base = json.loads(json.dumps(PRESETS[raw["preset"]]))
        else:
            hosts = raw.get("allowedHosts")
            if not isinstance(hosts, list) or not hosts or len(hosts) > 6:
                raise OAuthError(f"oauth provider {name}: a generic provider declares allowedHosts (exact host names)")
            base = {"authorizeUrl": raw.get("authorizeUrl"), "tokenUrl": raw.get("tokenUrl"), "revokeUrl": raw.get("revokeUrl"),
                    "allowedHosts": hosts, "authParams": {}, "implicitScopes": [], "benignScopes": [], "scopePrefixes": []}
        hosts = []
        for host in base["allowedHosts"]:
            try:
                parts = guard.parse_endpoint(f"https://{host}/") if isinstance(host, str) else None
            except guard.EndpointError as exc:
                raise OAuthError(f"oauth provider {name}: allowedHosts: {exc}") from None
            if parts is None or parts["host"] != host or parts["ipLiteral"]:
                raise OAuthError(f"oauth provider {name}: allowedHosts are exact public host names, no wildcards or addresses")
            hosts.append(host)
        endpoints: dict[str, str | None] = {}
        for key in ("authorizeUrl", "tokenUrl", "revokeUrl"):
            url = base.get(key)
            if url is None and key == "revokeUrl":
                endpoints[key] = None
                continue
            try:
                parts = guard.parse_endpoint(url, egress=hosts)
            except guard.EndpointError as exc:
                raise OAuthError(f"oauth provider {name}: {key}: {exc}") from None
            endpoints[key] = url
        extra = raw.get("authParams", {})
        params = dict(base["authParams"])
        if not isinstance(extra, Mapping) or set(extra) - set(AUTH_PARAMS) or not all(isinstance(v, str) and _PARAM_VALUE.match(v) for v in extra.values()):
            raise OAuthError(f"oauth provider {name}: authParams holds only: {', '.join(AUTH_PARAMS)}")
        params.update(extra)
        scopes_by_tool = raw.get("scopesByTool")
        if not isinstance(scopes_by_tool, Mapping) or not scopes_by_tool or len(scopes_by_tool) > 50:
            raise OAuthError(f"oauth provider {name}: scopesByTool maps each approved Tool to the OAuth scopes it needs")
        mapped = {}
        for tool, scopes in scopes_by_tool.items():
            if not isinstance(tool, str) or not _TOOL.match(tool):
                raise OAuthError(f"oauth provider {name}: scopesByTool keys are Tool names such as calendar.read")
            mapped[tool] = _scope_list(scopes, f"oauth provider {name}: scopesByTool.{tool}")
        providers[name] = {
            "name": name, "clientId": client_id, **endpoints, "allowedHosts": hosts, "authParams": params, "scopesByTool": mapped,
            "implicitScopes": _scope_list(raw.get("implicitScopes", base["implicitScopes"]), "implicitScopes", allow_empty=True),
            "benignScopes": _scope_list(raw.get("benignScopes", base["benignScopes"]), "benignScopes", allow_empty=True),
            "scopePrefixes": sorted(set(raw.get("scopePrefixes", base["scopePrefixes"]))),
        }
    return providers


# -- a guarded HTTPS transport --------------------------------------------------------------------------------------------

def system_resolver(host: str) -> list[str]:
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except OSError:
        raise OAuthError("the provider host did not resolve") from None


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class GuardedHttp:
    """POSTs to a provider endpoint only if it is https on an allowed host and resolves to public addresses (pinned); no redirects."""

    def __init__(self, hosts: Iterable[str], *, resolver: Callable[[str], Iterable[str]] = system_resolver,
                 connect: Callable[[str, str, float], Any] | None = None) -> None:
        self._hosts = tuple(hosts)
        self._resolver = resolver
        self._connect = connect or (lambda host, ip, timeout: _PinnedConnection(host, ip, timeout))

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        guard = _guard()
        try:
            parts = guard.qualify(url, self._resolver, egress=self._hosts)
        except guard.EndpointError as exc:
            raise OAuthError(f"the provider endpoint is not allowed: {exc}") from None
        connection = self._connect(parts["host"], parts["pinned"][0], HTTP_TIMEOUT)
        try:
            connection.request(method, parts["path"], body=body, headers=dict(headers))
            response = connection.getresponse()
            return response.status, response.read(MAX_RESPONSE + 1)[:MAX_RESPONSE]
        except (OSError, http.client.HTTPException) as exc:
            raise OAuthError("the provider could not be reached") from exc
        finally:
            connection.close()


# -- helpers --------------------------------------------------------------------------------------------------------------

def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(*parts: str) -> str:
    """A short, non-reversible label for a client id or a scope set, for ledger entries (never used for a token)."""
    return _digest("\x00".join(parts))[:16]


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _check(value: Any, pattern: re.Pattern[str], message: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise OAuthError(message)
    return value


def _norm(scope: str, prefixes: Iterable[str]) -> str:
    text = scope.casefold()
    for prefix in prefixes:
        if text.startswith(prefix.casefold()):
            text = text[len(prefix):]
    return text


def _scope_set(value: Any) -> set[str] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("scope")
    return {item for item in value.replace(",", " ").split() if item}


class LazyGrants:
    """The tenant's grant registry if one exists, so provider health follows the token without creating an empty registry."""

    def __init__(self, root: pathlib.Path, tenant: str, audit: Audit | None = None) -> None:
        self.root, self.tenant, self._audit = pathlib.Path(root), tenant, audit

    def _registry(self):
        if not (self.root / f"{self.tenant}.grants.json").is_file():
            return None
        return _sibling("model_garden_oauth_provider_grants", "provider_grants.py").GrantRegistry(self.root, tenant=self.tenant, audit=self._audit)

    def set_health(self, agent: str, connection_id: str, health: str) -> None:
        registry = self._registry()
        if registry is None:
            return
        for grant in registry.list(agent=agent):
            if grant["connectionId"] == connection_id and not grant.get("revokedAt") and grant["health"] != health:
                try:
                    registry.set_health(grant["id"], health, by="runtime:oauth")
                except PermissionError:
                    pass


# -- the store ------------------------------------------------------------------------------------------------------------

class OAuthConsentStore:
    def __init__(
        self, root: pathlib.Path, *, tenant: str, vault: Any, providers: Mapping[str, Mapping[str, Any]], redirect_uri: str,
        client_secret: Callable[[str], str], http: Http, audit: Audit | None = None, grants: Any | None = None,
    ) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise OAuthError("tenant must be a lowercase client-environment slug such as acme-prod")
        if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
            raise OAuthError("the redirect URI is the gateway's own https address")
        self.tenant, self.root = tenant, pathlib.Path(root)
        self.path = self.root / f"{tenant}.oauth-consents.json"
        self._vault, self._providers, self._redirect = vault, dict(providers), redirect_uri
        self._client_secret, self._http, self._audit, self._grants = client_secret, http, audit, grants

    # -- file ------------------------------------------------------------------------------------------------------------

    def _empty(self) -> dict[str, Any]:
        return {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "requests": {}, "states": {}, "connections": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
            raise OAuthError("oauth store has an unsupported format or belongs to another tenant")
        return loaded

    @contextlib.contextmanager
    def _flock(self, name: str) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        descriptor = os.open(self.root / f".{self.tenant}.{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        with self._flock("consents"):
            state = self._load()
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

    def _note(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def _base(self, agent: str, connection_id: str) -> str:
        return f"secret://{self.tenant}/{agent}/{connection_id}.oauth"

    def stable_ref(self, agent: str, connection_id: str) -> str:
        """The reference a grant and a connection declaration name; it resolves to the current access token in the sidecar."""
        return self._base(agent, connection_id)

    @staticmethod
    def _key(agent: str, connection_id: str) -> str:
        return f"{agent}/{connection_id}"

    def _provider(self, name: Any) -> Mapping[str, Any]:
        provider = self._providers.get(name) if isinstance(name, str) else None
        if provider is None:
            raise OAuthError("that provider is not declared for this deployment")
        return provider

    @staticmethod
    def _purge(state: dict[str, Any], now: int) -> None:
        state["states"] = {k: v for k, v in state["states"].items() if v["expiresAt"] > now - 3600}
        state["requests"] = {k: v for k, v in state["requests"].items() if v["expiresAt"] > now - 86400}

    # -- 1. the operator opens a request --------------------------------------------------------------------------------

    def request(self, *, agent: str, connection_id: str, provider: str, tools: Iterable[str], owner: str, requested_by: str, now: int,
                ttl_seconds: int = REQUEST_TTL) -> dict[str, Any]:
        """Open a single-use consent request for the named owner; the returned ``token`` is shown here once and never stored."""
        _check(agent, _AGENT, "choose the specific coworker this consent is for")
        _check(connection_id, _CONNECTION, "connection id must be a short identifier")
        _check(owner, _PERSON, "the owner must be a named person")
        _check(requested_by, _PERSON, "requested_by must be a named person")
        config = self._provider(provider)
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= MAX_REQUEST_TTL:
            raise OAuthError(f"a consent request lives between 60 and {MAX_REQUEST_TTL} seconds")
        wanted = sorted(set(tools))
        if not wanted or len(wanted) > 20 or not all(isinstance(tool, str) and _TOOL.match(tool) for tool in wanted):
            raise OAuthError("name the approved Tools this consent is for, such as calendar.read")
        scopes: set[str] = set(config["implicitScopes"])
        for tool in wanted:
            if tool not in config["scopesByTool"]:
                raise OAuthError(f"no OAuth scope is declared for {tool}, so it cannot be consented to")
            scopes.update(config["scopesByTool"][tool])
        token = secrets.token_urlsafe(32)
        request_id = "oc-" + secrets.token_hex(6)
        with self._locked() as state:
            self._purge(state, now)
            live = [item for item in state["requests"].values() if item["state"] == "pending" and item["expiresAt"] > now]
            if len(live) >= MAX_LIVE_REQUESTS:
                raise OAuthError("too many open consent requests; let them expire")
            for item in live:
                if (item["agent"], item["connectionId"]) == (agent, connection_id):
                    item["state"] = "cancelled"
            state["requests"][request_id] = {
                "id": request_id, "tokenDigest": _digest(token), "agent": agent, "connectionId": connection_id, "provider": provider,
                "owner": owner, "tools": wanted, "scopes": sorted(scopes), "requestedBy": requested_by, "createdAt": now,
                "expiresAt": now + ttl_seconds, "state": "pending", "starts": 0,
            }
        self._note("oauth.consent-requested", requested_by, {
            "requestId": request_id, "agent": agent, "connectionId": connection_id, "provider": provider, "owner": owner,
            "tools": wanted, "scopes": sorted(scopes), "expiresAt": now + ttl_seconds})
        return {"requestId": request_id, "token": token, "expiresAt": now + ttl_seconds, "scopes": sorted(scopes), "tools": wanted,
                "secretRef": self._base(agent, connection_id)}

    def cancel(self, request_id: str, *, by: str) -> None:
        _check(by, _PERSON, "by must be a named person")
        with self._locked() as state:
            item = state["requests"].get(request_id)
            if item is None or item["state"] != "pending":
                raise OAuthError("no such open consent request")
            item["state"] = "cancelled"
        self._note("oauth.consent-cancelled", by, {"requestId": request_id})

    # -- 2. the owner sees the consent page and approves ----------------------------------------------------------------

    def _match(self, state: dict[str, Any], token: Any, session_user: str, now: int) -> tuple[dict[str, Any] | None, str]:
        if not isinstance(token, str) or not _TOKEN.match(token):
            return None, "malformed-token"
        digest = _digest(token)
        item = next((i for i in state["requests"].values() if hmac.compare_digest(i["tokenDigest"], digest)), None)
        if item is None:
            return None, "unknown-token"
        if item["state"] != "pending":
            return item, item["state"]
        if item["expiresAt"] <= now:
            item["state"] = "expired"
            return item, "expired"
        if item["owner"].casefold() != session_user.casefold():
            return item, "wrong-person"
        return item, ""

    def _refuse(self, session_user: str, reason: str, item: Mapping[str, Any] | None, action: str = "oauth.consent-rejected") -> OAuthError:
        self._note(action, session_user, {"reason": reason, **({"requestId": item["id"]} if item else {})})
        return OAuthError("this link is not valid" if reason != "provider-unavailable" else "the provider could not be reached; try again")

    def preview(self, token: Any, *, session_user: str, now: int) -> dict[str, Any]:
        """What the consent page shows the signed-in owner: the provider, the Tools and every scope. Changes nothing."""
        _check(session_user, _PERSON, "the session must name a person")
        with self._locked() as state:
            item, reason = self._match(state, token, session_user, now)
            snapshot = dict(item) if item else None
        if reason:
            raise self._refuse(session_user, reason, snapshot)
        config = self._provider(snapshot["provider"])
        return {"requestId": snapshot["id"], "provider": snapshot["provider"], "connectionId": snapshot["connectionId"], "agent": snapshot["agent"],
                "tools": snapshot["tools"], "scopes": snapshot["scopes"], "expiresAt": snapshot["expiresAt"], "host": config["allowedHosts"][0]}

    def start(self, token: Any, *, session_user: str, now: int) -> dict[str, Any]:
        """The owner approved: mint a single-use state and PKCE verifier and return the provider's authorize URL (pinned host)."""
        _check(session_user, _PERSON, "the session must name a person")
        raw_state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with self._locked() as state:
            self._purge(state, now)
            item, reason = self._match(state, token, session_user, now)
            if not reason and item["starts"] >= MAX_STARTS:
                reason = "too-many-starts"
            if not reason and len([s for s in state["states"].values() if not s["consumed"] and s["expiresAt"] > now]) >= MAX_STATES:
                reason = "too-many-states"
            if not reason:
                state["states"] = {k: v for k, v in state["states"].items() if v["requestId"] != item["id"]}  # one live state per request
                state["states"][_digest(raw_state)] = {
                    "requestId": item["id"], "owner": item["owner"], "agent": item["agent"], "connectionId": item["connectionId"],
                    "provider": item["provider"], "verifier": verifier, "createdAt": now, "expiresAt": now + STATE_TTL, "consumed": False}
                item["starts"] += 1
            snapshot = dict(item) if item else None
        if reason:
            raise self._refuse(session_user, reason, snapshot)
        config = self._provider(snapshot["provider"])
        params = {
            "response_type": "code", "client_id": config["clientId"], "redirect_uri": self._redirect, "scope": " ".join(snapshot["scopes"]),
            "state": raw_state, "code_challenge": _pkce_challenge(verifier), "code_challenge_method": "S256", **config["authParams"],
        }
        url = config["authorizeUrl"] + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        self._note("oauth.consent-started", session_user, {"requestId": snapshot["id"], "provider": snapshot["provider"], "scopes": snapshot["scopes"],
                                                           "scopesFingerprint": fingerprint(*snapshot["scopes"])})
        return {"redirect": url, "expiresAt": now + STATE_TTL}

    # -- 3. the provider redirects back; the owner's session completes the consent --------------------------------------

    def _post(self, url: str, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        body = urllib.parse.urlencode(form).encode("ascii")
        status, raw = self._http("POST", url, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, body)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {}
        return status, payload if isinstance(payload, dict) else {}

    def _claim_state(self, raw_state: Any, session_user: str, now: int) -> tuple[dict[str, Any] | None, str]:
        if not isinstance(raw_state, str) or not _TOKEN.match(raw_state):
            return None, "malformed-state"
        with self._locked() as state:
            self._purge(state, now)
            record = state["states"].get(_digest(raw_state))
            if record is None:
                return None, "unknown-state"
            request = state["requests"].get(record["requestId"])
            if record["consumed"]:
                return record, "replay"
            if record["expiresAt"] <= now:
                record["consumed"] = True
                return record, "expired-state"
            if record["owner"].casefold() != session_user.casefold():
                return record, "wrong-person"  # left unspent: a different signed-in person cannot burn the owner's flow
            record["consumed"] = True
            if request is None or request["state"] != "pending":
                return record, "request-closed"
            return {**record, "scopes": list(request["scopes"]), "tools": list(request["tools"])}, ""

    def decline(self, raw_state: Any, error: Any, *, session_user: str, now: int) -> None:
        """The provider reported an error (for example ``access_denied``) on the callback; spend the state and say so."""
        _check(session_user, _PERSON, "the session must name a person")
        record, reason = self._claim_state(raw_state, session_user, now)
        code = error if isinstance(error, str) and _ERROR.match(error) else "error"
        if reason:
            raise self._refuse(session_user, reason, None)
        self._note("oauth.consent-declined", session_user, {"requestId": record["requestId"], "provider": record["provider"], "providerError": code})
        raise OAuthError("access was not granted at the provider")

    def complete(self, raw_state: Any, code: Any, *, session_user: str, now: int) -> dict[str, Any]:
        """Exchange the code (with the PKCE verifier) and store the tokens in the vault. The result holds no token."""
        _check(session_user, _PERSON, "the session must name a person")
        if not isinstance(code, str) or not _CODE.match(code):
            raise self._refuse(session_user, "malformed-code", None)
        record, reason = self._claim_state(raw_state, session_user, now)
        if reason:
            raise self._refuse(session_user, reason, None)
        config = self._provider(record["provider"])
        agent, connection_id = record["agent"], record["connectionId"]
        try:
            status, payload = self._post(config["tokenUrl"], {
                "grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect, "client_id": config["clientId"],
                "client_secret": self._client_secret(config["name"]), "code_verifier": record["verifier"]})
        except OAuthError:
            raise self._refuse(session_user, "provider-unavailable", {"id": record["requestId"]}) from None
        tokens = self._tokens(payload) if status == 200 else None
        if tokens is None:
            error = payload.get("error") if isinstance(payload.get("error"), str) and _ERROR.match(payload["error"]) else f"http-{status}"
            self._note("oauth.consent-rejected", session_user, {"reason": "exchange-failed", "providerError": error, "requestId": record["requestId"]})
            raise OAuthError("the provider did not accept the authorization; start again from the link")
        problem = self._scope_problem(config, record["scopes"], tokens["scopes"])
        if problem is None and not tokens["refresh"]:
            problem = "no-refresh-token"
        if problem is not None:
            self._revoke_at_provider(config, tokens["refresh"] or tokens["access"])
            self._note("oauth.consent-rejected", session_user, {"reason": problem, "requestId": record["requestId"], "provider": config["name"],
                                                                "requestedScopesFingerprint": fingerprint(*record["scopes"]),
                                                                "grantedScopesFingerprint": fingerprint(*sorted(tokens["scopes"]))})
            raise OAuthError("the provider granted different access than was asked for, so nothing was saved")
        return self._store_consent(record, config, tokens, session_user, now)

    @staticmethod
    def _tokens(payload: Mapping[str, Any]) -> dict[str, Any] | None:
        access, refresh = payload.get("access_token"), payload.get("refresh_token")
        kind = payload.get("token_type", "Bearer")
        expires = payload.get("expires_in", DEFAULT_ACCESS_TTL)
        if not isinstance(access, str) or not access or len(access) > 4000 or not isinstance(kind, str) or kind.casefold() != "bearer":
            return None
        if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 4000):
            return None
        if isinstance(expires, str) and expires.isdigit():
            expires = int(expires)
        if not isinstance(expires, int) or isinstance(expires, bool) or not 60 <= expires <= 86400:
            return None
        try:
            granted = _scope_set(payload.get("scope"))
        except ValueError:
            return None
        return {"access": access, "refresh": refresh, "expiresIn": expires, "scopes": granted}

    @staticmethod
    def _scope_problem(config: Mapping[str, Any], requested: list[str], granted: set[str] | None) -> str | None:
        """``None`` when the provider granted exactly what was asked (a missing ``scope`` field means as requested)."""
        if granted is None:
            return None
        prefixes = config["scopePrefixes"]
        asked = {_norm(s, prefixes) for s in requested}
        got = {_norm(s, prefixes) for s in granted}
        benign = {_norm(s, prefixes) for s in config["benignScopes"]}
        if got - asked - benign:
            return "scope-widened"
        if asked - got:
            return "scope-narrowed"
        return None

    def _store_consent(self, record: Mapping[str, Any], config: Mapping[str, Any], tokens: Mapping[str, Any], owner: str, now: int) -> dict[str, Any]:
        agent, connection_id = record["agent"], record["connectionId"]
        key = self._key(agent, connection_id)
        base = self._base(agent, connection_id)
        with self._locked() as state:
            previous = state["connections"].get(key)
            av = (previous["accessVersion"] if previous else 0) + 1
            rv = (previous["refreshVersion"] if previous else 0) + 1
            access_ref, refresh_ref = f"{base}/v{av}", f"{base}-refresh/v{rv}"
            try:
                self._vault.put(access_ref, tokens["access"])
                self._vault.put(refresh_ref, tokens["refresh"])
            except Exception as exc:  # noqa: BLE001 - nothing about the tokens leaks; the old consent stays as it was
                with contextlib.suppress(Exception):
                    self._vault.delete(access_ref)
                    self._vault.delete(refresh_ref)
                self._revoke_at_provider(config, tokens["refresh"])
                self._note("oauth.consent-rejected", owner, {"reason": "vault-refused", "error": type(exc).__name__, "requestId": record["requestId"]})
                raise OAuthError("the access could not be stored; try again") from None
            granted = sorted(tokens["scopes"]) if tokens["scopes"] is not None else list(record["scopes"])
            state["connections"][key] = {
                "agent": agent, "connectionId": connection_id, "provider": config["name"], "owner": owner, "tools": list(record["tools"]),
                "scopes": list(record["scopes"]), "accessVersion": av, "refreshVersion": rv, "accessRef": access_ref, "refreshRef": refresh_ref,
                "accessExpiresAt": now + tokens["expiresIn"], "consentedAt": now, "consentedBy": owner, "consentRequestId": record["requestId"],
                "status": "active", "revokedAt": None, "lastRefreshAt": None, "failure": None,
                "clientIdFingerprint": fingerprint(config["clientId"]), "grantedScopesFingerprint": fingerprint(*granted)}
            item = state["requests"].get(record["requestId"])
            if item is not None:
                item["state"] = "used"
        for stale in (previous["accessRef"], previous["refreshRef"]) if previous and previous.get("status") in ("active", "failed") else ():
            with contextlib.suppress(Exception):
                self._vault.delete(stale)
        self._set_grant_health(agent, connection_id, "healthy")
        self._note("oauth.consent-granted", owner, {
            "attestation": "owner-session", "attestedBy": owner, "verifiedVia": "web-signin", "operatorAttested": False,
            "requestId": record["requestId"], "agent": agent, "connectionId": connection_id, "provider": config["name"],
            "tools": list(record["tools"]), "scopes": list(record["scopes"]), "clientIdFingerprint": fingerprint(config["clientId"]),
            "grantedScopesFingerprint": fingerprint(*granted), "accessVersion": av, "refreshVersion": rv})
        return {"ok": True, "provider": config["name"], "scopes": list(record["scopes"]), "tools": list(record["tools"]),
                "connectionId": connection_id, "secretRef": base}

    # -- using, refreshing and ending a consent -----------------------------------------------------------------------

    def connection_for_ref(self, reference: str) -> tuple[str, str] | None:
        """``(agent, connection)`` if ``reference`` is the stable access reference of a consented connection (lock-free read)."""
        prefix = f"secret://{self.tenant}/"
        if not isinstance(reference, str) or not reference.startswith(prefix) or not reference.endswith(".oauth"):
            return None
        try:
            connections = self._load()["connections"]
        except (OSError, ValueError, OAuthError):
            return None
        for record in connections.values():
            if self._base(record["agent"], record["connectionId"]) == reference:
                return record["agent"], record["connectionId"]
        return None

    def status(self, agent: str, connection_id: str, *, now: int) -> dict[str, Any]:
        record = self._load()["connections"].get(self._key(agent, connection_id))
        if record is None:
            return {"state": "unset"}
        shown = {"state": record["status"], "provider": record["provider"], "tools": record["tools"], "scopes": record["scopes"],
                 "consentedBy": record["consentedBy"], "consentedAt": record["consentedAt"], "lastRefreshAt": record["lastRefreshAt"],
                 "grantedScopesFingerprint": record["grantedScopesFingerprint"]}
        if record["status"] == "active":
            shown["accessExpiresInSeconds"] = max(0, record["accessExpiresAt"] - now)
        if record.get("failure"):
            shown["failure"] = record["failure"]
        if record.get("revokedAt"):
            shown["revokedAt"] = record["revokedAt"]
        return shown

    def _set_grant_health(self, agent: str, connection_id: str, health: str) -> None:
        if self._grants is not None:
            with contextlib.suppress(Exception):
                self._grants.set_health(agent, connection_id, health)

    def _revoke_at_provider(self, config: Mapping[str, Any], token: str | None) -> bool | None:
        """Best-effort RFC 7009 revocation. ``None`` when the provider declares no endpoint."""
        if not config.get("revokeUrl") or not token:
            return None
        try:
            status, _ = self._post(config["revokeUrl"], {"token": token, "client_id": config["clientId"],
                                                          "client_secret": self._client_secret(config["name"])})
        except Exception:  # noqa: BLE001
            return False
        return 200 <= status < 300

    def _terminate(self, key: str, status: str, reason: str, now: int, *, record: Mapping[str, Any], delete_tokens: bool) -> None:
        if delete_tokens:
            for ref in (record["accessRef"], record["refreshRef"]):
                with contextlib.suppress(Exception):
                    self._vault.delete(ref)
        with self._locked() as state:
            current = state["connections"].get(key)
            if current is not None and current["refreshVersion"] == record["refreshVersion"]:
                current.update(status=status, failure=reason, revokedAt=now if delete_tokens else None)
        self._set_grant_health(record["agent"], record["connectionId"], "failed")
        self._note("oauth.revoked-by-provider" if status == "revoked-at-provider" else "oauth.refresh-failed", "runtime:oauth", {
            "agent": record["agent"], "connectionId": record["connectionId"], "provider": record["provider"], "reason": reason, "state": status})

    def ensure_fresh(self, agent: str, connection_id: str, *, read: Callable[[str], str], now: int, force: bool = False) -> None:
        """Make sure the stable access reference resolves to an unexpired token. Raises ``OAuthError`` if it cannot.

        ``read`` opens vault references; only the governed sidecar has one. Everything else here is credential-free.
        """
        key = self._key(agent, connection_id)
        record = self._load()["connections"].get(key)
        if record is None:
            raise OAuthError("no consent is recorded for that connection")
        if record["status"] != "active":
            raise OAuthError(f"this connection is {record['status']}; the owner must consent again")
        if not force and record["accessExpiresAt"] - now > REFRESH_SKEW:
            return
        with self._flock(f"refresh-{hashlib.sha256(key.encode()).hexdigest()[:16]}"):
            record = self._load()["connections"].get(key)
            if record is None or record["status"] != "active":
                raise OAuthError("this connection is no longer active")
            if not force and record["accessExpiresAt"] - now > REFRESH_SKEW:
                return  # another process refreshed while this one waited
            config = self._provider(record["provider"])
            try:
                refresh_token = read(record["refreshRef"])
            except Exception:  # noqa: BLE001
                self._terminate(key, "failed", "refresh-token-unreadable", now, record=record, delete_tokens=False)
                raise OAuthError("the stored access cannot be opened; the owner must consent again") from None
            try:
                status, payload = self._post(config["tokenUrl"], {
                    "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": config["clientId"],
                    "client_secret": self._client_secret(config["name"])})
            except Exception:  # noqa: BLE001
                status, payload = 0, {}
            error = payload.get("error") if isinstance(payload.get("error"), str) and _ERROR.match(payload["error"]) else ""
            if status != 200:
                if status in (400, 401) and error in REVOKED_AT_PROVIDER:
                    self._terminate(key, "revoked-at-provider", error, now, record=record, delete_tokens=True)
                    raise OAuthError("access was revoked at the provider; the owner must consent again")
                if status in (400, 401, 403) and error not in RETRYABLE and status != 0 and error:
                    self._terminate(key, "failed", error, now, record=record, delete_tokens=False)
                    raise OAuthError("the provider refused to refresh this access; an operator needs to look at it")
                self._set_grant_health(agent, connection_id, "degraded")
                self._note("oauth.refresh-unavailable", "runtime:oauth", {"agent": agent, "connectionId": connection_id, "provider": record["provider"],
                                                                        "httpStatus": status})
                raise OAuthError("the provider is unavailable; try again shortly")
            tokens = self._tokens(payload)
            if tokens is None:
                self._set_grant_health(agent, connection_id, "degraded")
                raise OAuthError("the provider returned an unusable answer")
            problem = self._scope_problem(config, record["scopes"], tokens["scopes"])
            if problem is not None:
                self._revoke_at_provider(config, tokens["refresh"] or tokens["access"])
                self._terminate(key, "failed", problem, now, record=record, delete_tokens=True)
                raise OAuthError("the provider now grants different access than was approved; the owner must consent again")
            self._rotate(key, record, tokens, refresh_token, now)

    def _rotate(self, key: str, record: Mapping[str, Any], tokens: Mapping[str, Any], old_refresh: str, now: int) -> None:
        base = self._base(record["agent"], record["connectionId"])
        av = record["accessVersion"] + 1
        access_ref = f"{base}/v{av}"
        new_refresh = tokens["refresh"] if tokens["refresh"] and tokens["refresh"] != old_refresh else None
        rv = record["refreshVersion"] + (1 if new_refresh else 0)
        refresh_ref = f"{base}-refresh/v{rv}" if new_refresh else record["refreshRef"]
        try:
            self._vault.put(access_ref, tokens["access"])
            if new_refresh:
                self._vault.put(refresh_ref, new_refresh)  # the rotated refresh token is stored before the old one is deleted
        except Exception:  # noqa: BLE001
            self._set_grant_health(record["agent"], record["connectionId"], "degraded")
            raise OAuthError("the refreshed access could not be stored") from None
        with self._locked() as state:
            current = state["connections"].get(key)
            if current is None or current["refreshVersion"] != record["refreshVersion"] or current["accessVersion"] != record["accessVersion"]:
                for ref in (access_ref, refresh_ref if new_refresh else None):
                    if ref:
                        with contextlib.suppress(Exception):
                            self._vault.delete(ref)
                raise OAuthError("the consent changed while refreshing; try again")
            current.update(accessVersion=av, refreshVersion=rv, accessRef=access_ref, refreshRef=refresh_ref,
                           accessExpiresAt=now + tokens["expiresIn"], lastRefreshAt=now, failure=None)
        for stale in (record["accessRef"], record["refreshRef"] if new_refresh else None):
            if stale:
                with contextlib.suppress(Exception):
                    self._vault.delete(stale)
        self._set_grant_health(record["agent"], record["connectionId"], "healthy")
        self._note("oauth.refreshed", "runtime:oauth", {"agent": record["agent"], "connectionId": record["connectionId"], "provider": record["provider"],
                                                       "refreshRotated": bool(new_refresh), "accessVersion": av})

    def revoke(self, agent: str, connection_id: str, *, by: str, now: int, read: Callable[[str], str] | None = None) -> dict[str, Any]:
        """Offboard one consent: revoke at the provider when possible, then delete both tokens from the vault."""
        _check(by, _PERSON, "by must be a named person")
        key = self._key(agent, connection_id)
        record = self._load()["connections"].get(key)
        if record is None or record["status"] in ("revoked", "revoked-at-provider"):
            raise OAuthError("there is no active consent for that connection")
        config = self._provider(record["provider"])
        provider_revoked: bool | None = None
        if read is not None:
            try:
                provider_revoked = self._revoke_at_provider(config, read(record["refreshRef"]))
            except Exception:  # noqa: BLE001
                provider_revoked = False
        try:
            self._vault.delete(record["accessRef"])
            self._vault.delete(record["refreshRef"])
        except Exception as exc:  # noqa: BLE001 - not revoked until the vault says so
            self._note("oauth.revoke-failed", by, {"agent": agent, "connectionId": connection_id, "error": type(exc).__name__})
            raise OAuthError("the vault did not delete the tokens; they are still stored") from exc
        with self._locked() as state:
            current = state["connections"][key]
            current.update(status="revoked", revokedAt=now, failure=None)
            for item in state["requests"].values():
                if (item["agent"], item["connectionId"]) == (agent, connection_id) and item["state"] == "pending":
                    item["state"] = "cancelled"
        self._set_grant_health(agent, connection_id, "failed")
        self._note("oauth.revoked", by, {"agent": agent, "connectionId": connection_id, "provider": record["provider"],
                                         "providerRevoked": provider_revoked})
        note = ("revoked at the provider" if provider_revoked else "NOT revoked at the provider: revoke it in the provider's account or admin console"
                if provider_revoked is False else "deleted locally; this provider declares no revocation endpoint or the vault key was not available, so revoke it at the provider")
        return {"state": "revoked", "providerRevoked": provider_revoked, "note": note}

    def revoke_all(self, *, by: str, now: int, agent: str | None = None, read: Callable[[str], str] | None = None) -> list[dict[str, Any]]:
        """Offboarding: revoke every active consent of one coworker (or all). Failures are reported per connection."""
        results = []
        for record in sorted(self._load()["connections"].values(), key=lambda r: (r["agent"], r["connectionId"])):
            if (agent is None or record["agent"] == agent) and record["status"] in ("active", "failed"):
                try:
                    outcome = self.revoke(record["agent"], record["connectionId"], by=by, now=now, read=read)
                except OAuthError as exc:
                    outcome = {"state": "error", "error": str(exc)}
                results.append({"agent": record["agent"], "connectionId": record["connectionId"], **outcome})
        return results


class RefreshingResolver:
    """Wraps the sidecar's secret resolver: a stable OAuth access reference is refreshed on use before it is resolved."""

    def __init__(self, inner: Callable[[str], str], store: OAuthConsentStore, read: Callable[[str], str], clock: Callable[[], float]) -> None:
        self._inner, self._store, self._read, self._clock = inner, store, read, clock

    def __call__(self, reference: str) -> str:
        owner = self._store.connection_for_ref(reference)
        if owner is not None:
            self._store.ensure_fresh(owner[0], owner[1], read=self._read, now=int(self._clock()))
        return self._inner(reference)


def store_from_environment(environ: Mapping[str, str], *, vault: Any, http: Http | None = None, audit: Audit | None = None) -> OAuthConsentStore | None:
    """The sidecar's view of the OAuth store, from ``MODEL_GARDEN_OAUTH_CONFIG`` (the gateway's rendered configuration)."""
    config_path = environ.get("MODEL_GARDEN_OAUTH_CONFIG", "").strip()
    if not config_path:
        return None
    config = json.loads(pathlib.Path(config_path).read_text(encoding="utf-8"))
    oauth = config.get("oauth")
    if not isinstance(oauth, Mapping):
        return None
    if not environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST"):
        raise OAuthError("the capability manifest (MODEL_GARDEN_CAPABILITY_MANIFEST) is needed to know the client and environment")
    manifest = json.loads(pathlib.Path(environ["MODEL_GARDEN_CAPABILITY_MANIFEST"]).read_text(encoding="utf-8"))
    tenant = tenant_of_manifest(manifest)
    if tenant is None:
        raise OAuthError("the capability manifest must name the client and environment")
    providers = load_providers(oauth["providers"])
    root = pathlib.Path(environ.get("MODEL_GARDEN_OAUTH_ROOT") or pathlib.Path(environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")).parent)
    hosts = sorted({host for provider in providers.values() for host in provider["allowedHosts"]})

    def secret(provider: str) -> str:
        value = environ.get(client_secret_env(provider), "")
        if not value:
            raise OAuthError("the OAuth client secret is not available")
        return value

    return OAuthConsentStore(root / "oauth-consents", tenant=tenant, vault=vault, providers=providers,
                             redirect_uri=f"{config['publicOrigin']}/connections/oauth/callback", client_secret=secret,
                             http=http or GuardedHttp(hosts), audit=audit, grants=LazyGrants(root / "provider-grants", tenant, audit))
