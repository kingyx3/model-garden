"""The Employee Studio: the owner's page for seeing and safely changing a coworker (#130, #66, #86, #110, #111).

Served by the channel gateway under ``/studio`` (only when ``studio`` is configured), behind the web chat's verified session. Everyone
on the web allow-list can *read* the coworker (Skills, Tools, grants, readiness blockers, schedules, recent ledger activity, change
requests); only the configured owner can propose, approve, reject or roll back a change. All decisions are made by
``platform/core/change_requests.py``; this module is the HTTP surface and nothing more.

- **Authority from the session.** The role comes from the verified session's address, never from the request body. A change request
  is applied only by an explicit approve call from the named owner that names the exact digest of the diff they reviewed.
- **CSRF.** Every state-changing call must be a same-origin ``application/json`` POST: ``Origin`` equal to the configured public
  origin, and ``X-Studio-CSRF`` equal to a token derived (HMAC) from the caller's session, issued by ``GET /studio/api/state``.
  The session travels in an ``Authorization`` header, never a cookie, so a cross-site page cannot ride it either way.
- **Strict CSP, no CDN, no secrets.** Script and style come from this origin; the page writes server text with ``textContent``.
  The API returns names, digests, readiness text and non-secret grant summaries only. Request bodies are capped at 32 KB.

Not done: the live runtime does not read the overlay; applying records the decision and produces the workspace patch the operator
merges (see ``change_requests.py``).

Colleagues can hold roles (``platform/core/change_requests.ACCESS_ROLES``): the owner gives and takes them away through an ``access``
change request. A colleague with the ``author`` role may *propose* changes (never an ``access`` change) and still cannot approve, reject
or roll back; everyone else on the allow-list reads.

Configuration (``gateway.studio``): ``{"owner": "alice@client.example", "ownerKeys": {"alice@client.example": "<base64 Ed25519 public key>"},
"people": {"bob@client.example": ["author"]}}`` (``people`` is optional: the roles in place when the Studio starts).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib.util
import json
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAX_BODY = 32 * 1024
LEDGER_TAIL_BYTES = 256 * 1024
ACTIVITY_LIMIT = 15
_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
_PAGE_HEADERS = {"Content-Security-Policy": _CSP, "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                 "X-Content-Type-Options": "nosniff"}
_CHANGE = re.compile(r"^/studio/api/changes/(cr-[0-9a-f]{12})(?:/(approve|reject|rollback))?$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
CSRF_CONTEXT = b"model-garden-studio-csrf/1\n"


class StudioConfigError(ValueError):
    """The ``studio`` section is unusable; the message names the field."""


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _core():
    return _load("model_garden_studio_change_requests", "platform/core/change_requests.py")


def _assets():
    return _load("model_garden_studio_assets", "platform/runtime/studio_assets.py")


def check_config(section: Any) -> dict[str, Any]:
    """Validate ``gateway.studio``; keys are public only."""
    if not isinstance(section, Mapping) or set(section) - {"owner", "ownerKeys", "people"}:
        raise StudioConfigError("studio holds only owner, ownerKeys and people")
    owner = section.get("owner")
    if not isinstance(owner, str) or not _PERSON.match(owner.strip()):
        raise StudioConfigError("studio.owner must name the owner (their web sign-in address)")
    keys = section.get("ownerKeys") or {}
    if not isinstance(keys, Mapping):
        raise StudioConfigError("studio.ownerKeys maps an owner identity to a base64 Ed25519 public key")
    for identity, value in keys.items():
        try:
            valid = isinstance(identity, str) and isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32
        except (ValueError, binascii.Error):
            valid = False
        if not valid:
            raise StudioConfigError("studio.ownerKeys maps an owner identity to a base64 Ed25519 public key")
    people = section.get("people") or {}
    roles = _core().ACCESS_ROLES
    if not isinstance(people, Mapping) or not all(
        isinstance(person, str) and _PERSON.match(person.strip()) and person.strip().lower() != owner.strip().lower()
        and isinstance(held, list) and held and all(isinstance(role, str) and role in roles for role in held)
        for person, held in people.items()
    ):
        raise StudioConfigError("studio.people maps a colleague (not the owner) to roles from: " + ", ".join(sorted(roles)))
    return {"owner": owner.strip(), "ownerKeys": {k.strip().lower(): v for k, v in keys.items()},
            "people": {k.strip().lower(): sorted(set(v)) for k, v in people.items()}}


class Studio:
    """Routes ``/studio`` requests for one gateway. ``handle`` is a pure function of a request, so it is tested without a socket."""

    def __init__(self, gateway: Any, config: Mapping[str, Any]) -> None:
        self.gateway = gateway
        self.config = check_config(config)

    # -- helpers ------------------------------------------------------------------------------------------

    def _store(self):
        gateway = self.gateway
        manifest_path = gateway.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST", "")
        return _core().load_store(
            gateway.data, gateway.desired_state, tenant=gateway.tenant, studio=self.config, manifest_path=manifest_path or None,
            audit=gateway.audit, clock=gateway.clock, schedules=gateway._schedule_store().list,  # noqa: SLF001
            signin=lambda: ((gateway.config.get("web") or {}).get("allowedAddresses") or []),
        )

    def csrf_token(self, session: str) -> str:
        key = self.gateway._secret("MODEL_GARDEN_WEBCHAT_KEY").encode()  # noqa: SLF001
        return hmac.new(key, CSRF_CONTEXT + session.encode("utf-8"), hashlib.sha256).hexdigest()[:48]

    def _activity(self, agent: str) -> list[dict[str, Any]]:
        path = self.gateway.audit_path
        try:
            with open(path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - LEDGER_TAIL_BYTES))
                lines = handle.read().decode("utf-8", "replace").splitlines()
                if size > LEDGER_TAIL_BYTES:
                    lines = lines[1:]
        except OSError:
            return []
        found: list[dict[str, Any]] = []
        for raw in reversed(lines):
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            action = str(event.get("action") or "")
            studio_event = action.startswith("studio.")
            if not isinstance(event, dict) or not (event.get("agent") == agent or studio_event):
                continue
            row = {"timestamp": event.get("timestamp"), "event": event.get("event"), "action": action or None, "outcome": event.get("outcome"),
                   "tool": event.get("tool"), "by": event.get("performedBy") or event.get("initiatingUser")}
            if studio_event and isinstance(event.get("details"), Mapping):
                row["change"] = event["details"].get("id")
            found.append(row)
            if len(found) >= ACTIVITY_LIMIT:
                break
        return found

    @staticmethod
    def _public(change: Mapping[str, Any]) -> dict[str, Any]:
        """The change request as shown: the rollback snapshot (a copy of the whole overlay) stays on the host."""
        return {k: v for k, v in change.items() if k != "rollback"}

    # -- request handling ---------------------------------------------------------------------------------

    def handle(self, method: str, route: str, headers: Mapping[str, str], body: bytes) -> Any:
        ingress = _load("model_garden_gateway_ingress", "platform/channels/ingress.py")
        Response = ingress.Response
        lowered = {k.lower(): v for k, v in headers.items()}

        def reply(status: int, document: Mapping[str, Any]) -> Any:
            return Response(status, json.dumps(document, sort_keys=True).encode("utf-8"), headers={"Cache-Control": "no-store"})

        method = method.upper()
        if method == "GET" and route in ("/studio", "/studio/"):
            return Response(200, _assets().STUDIO_HTML.encode("utf-8"), "text/html; charset=utf-8", _PAGE_HEADERS)
        if method == "GET" and route == "/studio/app.js":
            return Response(200, _assets().STUDIO_JS.encode("utf-8"), "text/javascript; charset=utf-8", _PAGE_HEADERS)
        if method == "GET" and route == "/studio/app.css":
            return Response(200, _assets().STUDIO_CSS.encode("utf-8"), "text/css; charset=utf-8", _PAGE_HEADERS)
        if not route.startswith("/studio/api/") or method not in ("GET", "POST"):
            return reply(404, {"error": "not found"})
        verification = _load("model_garden_text_channel", "platform/channels/text_channel.py").ChannelVerificationError
        authorization = lowered.get("authorization", "")
        try:
            identity = self.gateway._web_identity(headers)  # noqa: SLF001
        except verification:
            return reply(401, {"error": "Sign in first."})
        session = authorization[7:].strip()
        user = identity.channel_user_id
        store = self._store()
        is_owner = user.strip().lower() == store.owner.lower()
        role = "owner" if is_owner else "author" if "author" in store.roles_of(user) else "viewer"
        if method == "GET":
            return self._read(route, store, reply, user, role, session)
        return self._write(route, store, reply, lowered, body, user, role, session)

    def _read(self, route: str, store: Any, reply: Any, user: str, role: str, session: str) -> Any:
        if route == "/studio/api/state":
            changes = [store.summary(c) for c in store.list()[:100]]
            return reply(200, {"user": user, "role": role, "owner": store.owner, "csrf": self.csrf_token(session), "coworkers": store.coworkers(),
                               "changes": changes, "activity": self._activity(store.agent),
                               "access": [{"person": person, "roles": roles} for person, roles in store.effective_access().items()],
                               "roles": _core().ACCESS_ROLES})
        match = _CHANGE.match(route)
        if match and not match.group(2):
            try:
                change = store.get(match.group(1))
            except _core().ChangeError:
                return reply(404, {"error": "no such change request"})
            shown = self._public(change)
            if change.get("state") == "proposed":
                shown["liveEvidence"] = store.evidence(change)
            return reply(200, {"change": shown})
        return reply(404, {"error": "not found"})

    def _write(self, route: str, store: Any, reply: Any, headers: Mapping[str, str], body: bytes, user: str, role: str, session: str) -> Any:
        core = _core()
        # Same origin and a per-session token first: a request that fails either never reaches the owner check or any state.
        origin = self.gateway.config.get("publicOrigin")
        if not origin or headers.get("origin") != origin:
            return reply(403, {"error": "Cross-origin request refused."})
        if not hmac.compare_digest(headers.get("x-studio-csrf", ""), self.csrf_token(session)):
            return reply(403, {"error": "Missing or wrong request token. Reload the page."})
        if not headers.get("content-type", "").lower().startswith("application/json"):
            return reply(415, {"error": "JSON only."})
        if len(body) > MAX_BODY:
            return reply(413, {"error": "That request is too large."})
        if role == "viewer" or (role == "author" and route != "/studio/api/changes"):
            return reply(403, {"error": "Only the owner can approve or change things here. You can read everything"
                                        + (" and propose changes." if role == "author" else ".")})
        try:
            request = json.loads(body or b"{}")
        except ValueError:
            return reply(400, {"error": "That is not valid JSON."})
        if not isinstance(request, dict):
            return reply(400, {"error": "That is not valid JSON."})
        try:
            if route == "/studio/api/changes":
                change = store.propose(str(request.get("kind", "")), request.get("params") or {}, by=user)
                return reply(200, {"change": self._public(change)})
            match = _CHANGE.match(route)
            if not match or not match.group(2):
                return reply(404, {"error": "not found"})
            change_id, action = match.group(1), match.group(2)
            if action == "approve":
                digest = request.get("digest")
                attachments = {"signoff": request["signoff"]} if isinstance(request.get("signoff"), dict) else {}
                change = store.approve(change_id, by=user, digest=digest if isinstance(digest, str) else "", attachments=attachments)
            elif action == "reject":
                change = store.reject(change_id, by=user, reason=str(request.get("reason") or ""))
            else:
                change = store.rollback(change_id, by=user)
            return reply(200, {"change": self._public(change)})
        except core.ChangeRefused as exc:
            return reply(403 if exc.reasons == ["not-the-owner"] else 409,
                         {"error": "That is not allowed yet: " + ", ".join(exc.reasons)[:300], "reasons": exc.reasons})
        except core.ChangeError as exc:
            return reply(400, {"error": str(exc)[:300]})
