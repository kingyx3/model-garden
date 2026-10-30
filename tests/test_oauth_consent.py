"""Provider OAuth consent end to end against a fake provider (#66, #86, #110, #129). No real network, provider, mail relay or token."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


oauth = load("oauth_consent_under_test", "platform/core/oauth_consent.py")
vault_module = load("sealed_vault_for_oauth", "platform/core/sealed_vault.py")
grants_module = load("provider_grants_for_oauth", "platform/core/provider_grants.py")
notices = load("connection_notices_for_oauth", "platform/core/connection_notices.py")
upstream_http = load("upstream_http_for_oauth", "platform/runtime/upstream_http.py")
gateway_module = load("gateway_for_oauth", "platform/runtime/gateway.py")
connection_cli = load("runtime_connection_for_oauth", "scripts/runtime-connection.py")
deployer = load("deployer_for_oauth", "scripts/deploy-client-docker.py")

KEYS = vault_module.generate_keypair()
NOW = 1_800_000_000
TENANT = "acme-dev"
OWNER = "owner@acme.example"
OPERATOR = "jordan@acme.example"
READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CLIENT_ID = "client-123.apps.googleusercontent.com"
CLIENT_SECRET = "client-secret-NEVER-LOGGED"
PROVIDERS = {"google": {"preset": "google", "clientId": CLIENT_ID,
                        "scopesByTool": {"calendar.read": [READ_SCOPE], "calendar.create": [READ_SCOPE, EVENTS_SCOPE]}}}
STABLE = f"secret://{TENANT}/receptionist/calendar.oauth"


class FakeProvider:
    """An in-memory Google-like authorization server: PKCE-checked single-use codes, rotating refresh tokens, revocation."""

    def __init__(self):
        self.codes, self.refresh, self.revoked, self.calls = {}, {}, [], []
        self.counter = 0
        self.rotate = False
        self.refresh_status = None  # force an HTTP status/error on refresh: (status, error)
        self.refresh_scope = None
        self.exchange_scope = None
        self.omit_refresh = False
        self.leaked = []

    def authorize(self, query, *, granted=None):
        self.counter += 1
        code = f"code-{self.counter}-abc"
        self.codes[code] = {"challenge": query["code_challenge"], "scope": granted if granted is not None else query["scope"], "redirect": query["redirect_uri"]}
        return code

    def _issue(self, scope):
        self.counter += 1
        access, refresh = f"access-token-{self.counter}-SECRET", f"refresh-token-{self.counter}-SECRET"
        self.leaked += [access, refresh]
        return access, refresh

    def __call__(self, method, url, headers, body):
        form = dict(urllib.parse.parse_qsl(body.decode("ascii")))
        self.calls.append((method, url, form))
        assert method == "POST" and headers["Content-Type"] == "application/x-www-form-urlencoded"
        if url == REVOKE_URL:
            self.revoked.append(form["token"])
            self.refresh.pop(form["token"], None)
            return 200, b"{}"
        assert url == TOKEN_URL
        assert form["client_id"] == CLIENT_ID and form["client_secret"] == CLIENT_SECRET
        if form["grant_type"] == "authorization_code":
            grant = self.codes.pop(form["code"], None)  # single use at the provider too
            if grant is None or grant["redirect"] != form["redirect_uri"]:
                return 400, b'{"error": "invalid_grant"}'
            expected = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if expected != grant["challenge"]:
                return 400, b'{"error": "invalid_grant"}'
            access, refresh = self._issue(grant["scope"])
            self.refresh[refresh] = grant["scope"]
            scope = self.exchange_scope if self.exchange_scope is not None else grant["scope"]
            document = {"access_token": access, "expires_in": 3600, "token_type": "Bearer", "scope": scope}
            if not self.omit_refresh:
                document["refresh_token"] = refresh
            return 200, json.dumps(document).encode()
        assert form["grant_type"] == "refresh_token"
        if self.refresh_status:
            status, error = self.refresh_status
            return status, json.dumps({"error": error}).encode() if error else b""
        scope = self.refresh.get(form["refresh_token"])
        if scope is None:
            return 400, b'{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'
        access, new_refresh = self._issue(scope)
        document = {"access_token": access, "expires_in": 3600, "token_type": "Bearer", "scope": self.refresh_scope or scope}
        if self.rotate:
            del self.refresh[form["refresh_token"]]
            self.refresh[new_refresh] = scope
            document["refresh_token"] = new_refresh
        return 200, json.dumps(document).encode()


def query_of(url):
    parts = urllib.parse.urlsplit(url)
    return parts, {key: values[0] for key, values in urllib.parse.parse_qs(parts.query).items()}


def tree_text(root: pathlib.Path) -> str:
    out = []
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.is_file():
            out.append(path.name + path.read_text(errors="replace"))
    return "".join(out)


class StoreCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.provider = FakeProvider()
        self.events = []
        audit = lambda action, by, details: self.events.append((action, by, details))  # noqa: E731
        self.vault = vault_module.SealedVault(self.root / "vault", public_key=KEYS["public"])
        self.reader = vault_module.SealedResolver(self.root / "vault", private_key=KEYS["private"])
        self.registry = grants_module.GrantRegistry(self.root / "provider-grants", tenant=TENANT)
        self.store = oauth.OAuthConsentStore(
            self.root / "oauth-consents", tenant=TENANT, vault=self.vault, providers=oauth.load_providers(PROVIDERS),
            redirect_uri="https://chat.acme.example/connections/oauth/callback", client_secret=lambda name: CLIENT_SECRET,
            http=self.provider, audit=audit, grants=oauth.LazyGrants(self.root / "provider-grants", TENANT, audit))

    def make_grant(self):
        return self.registry.create(
            agent="receptionist", family="calendar", provider="google", connection_id="calendar", isolation="own-identity",
            acting_identity="receptionist@acme.example", secret_ref=STABLE, scopes=["calendar.read"], owner=OWNER, by=OPERATOR, now=NOW)

    def grant_health(self):
        return self.registry.list(agent="receptionist")[0]["health"]

    def open(self, tools=("calendar.read",), now=NOW, owner=OWNER):
        return self.store.request(agent="receptionist", connection_id="calendar", provider="google", tools=tools, owner=owner,
                                  requested_by=OPERATOR, now=now)

    def approve(self, request, *, now=NOW, user=OWNER):
        started = self.store.start(request["token"], session_user=user, now=now)
        parts, query = query_of(started["redirect"])
        return parts, query

    def consent(self, tools=("calendar.read",), now=NOW):
        request = self.open(tools, now)
        _, query = self.approve(request, now=now)
        code = self.provider.authorize(query)
        return request, query, self.store.complete(query["state"], code, session_user=OWNER, now=now)

    def actions(self):
        return [action for action, _, _ in self.events]

    def assert_no_leak(self, *extra):
        everything = tree_text(self.root) + json.dumps(self.events, default=str)
        for secret in (*self.provider.leaked, CLIENT_SECRET, *extra):
            self.assertNotIn(secret, everything)


class ConsentFlowTests(StoreCase):
    def test_owner_consent_stores_tokens_only_in_the_vault_and_is_ledgered_as_owner_attested(self):
        self.make_grant()
        request = self.open()
        self.assertEqual(request["scopes"], [READ_SCOPE])
        self.assertEqual(request["secretRef"], STABLE)
        preview = self.store.preview(request["token"], session_user=OWNER, now=NOW)
        self.assertEqual((preview["provider"], preview["tools"], preview["scopes"]), ("google", ["calendar.read"], [READ_SCOPE]))
        parts, query = self.approve(request)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "accounts.google.com", "/o/oauth2/v2/auth"))
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual((query["response_type"], query["client_id"], query["scope"], query["access_type"]), ("code", CLIENT_ID, READ_SCOPE, "offline"))
        self.assertEqual(query["redirect_uri"], "https://chat.acme.example/connections/oauth/callback")
        result = self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW)
        self.assertEqual((result["provider"], result["scopes"], result["secretRef"]), ("google", [READ_SCOPE], STABLE))
        self.assertTrue(self.reader(STABLE).startswith("access-token-"))
        self.assertTrue(self.reader(STABLE.replace(".oauth", ".oauth-refresh")).startswith("refresh-token-"))
        granted = next(item for item in self.events if item[0] == "oauth.consent-granted")
        self.assertEqual(granted[1], OWNER)  # the owner's verified session, never the operator
        self.assertEqual((granted[2]["attestation"], granted[2]["operatorAttested"], granted[2]["verifiedVia"]), ("owner-session", False, "web-signin"))
        self.assertEqual(granted[2]["scopes"], [READ_SCOPE])
        self.assertEqual(len(granted[2]["clientIdFingerprint"]), 16)
        self.assertNotIn(CLIENT_ID, json.dumps(granted[2]))
        requested = next(item for item in self.events if item[0] == "oauth.consent-requested")
        self.assertEqual(requested[1], OPERATOR)
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW)["state"], "active")
        self.assert_no_leak(query["state"], request["token"])

    def test_scopes_are_exactly_what_the_approved_tools_need(self):
        self.assertEqual(self.open(("calendar.create",))["scopes"], sorted([READ_SCOPE, EVENTS_SCOPE]))
        self.assertEqual(self.open(("calendar.read",))["scopes"], [READ_SCOPE])
        with self.assertRaisesRegex(oauth.OAuthError, "no OAuth scope is declared for mail.send"):
            self.open(("mail.send",))
        with self.assertRaisesRegex(oauth.OAuthError, "approved Tools"):
            self.open(())
        with self.assertRaisesRegex(oauth.OAuthError, "not declared"):
            self.store.request(agent="receptionist", connection_id="calendar", provider="microsoft", tools=["calendar.read"], owner=OWNER,
                               requested_by=OPERATOR, now=NOW)

    def test_replay_is_refused_and_the_code_is_never_used_twice(self):
        request = self.open()
        _, query = self.approve(request)
        code = self.provider.authorize(query)
        self.store.complete(query["state"], code, session_user=OWNER, now=NOW)
        calls = len(self.provider.calls)
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.complete(query["state"], code, session_user=OWNER, now=NOW + 1)
        self.assertEqual(len(self.provider.calls), calls)  # no second exchange
        self.assertIn("replay", [d.get("reason") for a, _, d in self.events if a == "oauth.consent-rejected"])
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):  # the consent link itself is single use too
            self.store.start(request["token"], session_user=OWNER, now=NOW + 2)

    def test_a_failed_exchange_still_spends_the_state(self):
        request = self.open()
        _, query = self.approve(request)
        with self.assertRaisesRegex(oauth.OAuthError, "did not accept"):
            self.store.complete(query["state"], "code-from-nowhere", session_user=OWNER, now=NOW)
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW)

    def test_state_mismatch_unknown_and_malformed_state_are_refused_without_any_exchange(self):
        request = self.open()
        _, query = self.approve(request)
        code = self.provider.authorize(query)
        for bad in ("A" * 43, "short", None, query["state"][:-1] + ("A" if query["state"][-1] != "A" else "B")):
            with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
                self.store.complete(bad, code, session_user=OWNER, now=NOW)
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.complete(query["state"], "bad code with spaces", session_user=OWNER, now=NOW)
        self.assertEqual(self.provider.calls, [])
        # a second start replaces the first state, so the old redirect cannot be used
        again = self.store.start(request["token"], session_user=OWNER, now=NOW)
        _, newer = query_of(again["redirect"])
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.complete(query["state"], code, session_user=OWNER, now=NOW)
        self.assertEqual(self.store.complete(newer["state"], self.provider.authorize(newer), session_user=OWNER, now=NOW)["provider"], "google")

    def test_pkce_binds_the_exchange_to_the_verifier_held_by_the_gateway(self):
        request = self.open()
        _, query = self.approve(request)
        code = self.provider.authorize(query)
        self.store.complete(query["state"], code, session_user=OWNER, now=NOW)
        form = self.provider.calls[0][2]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, query["code_challenge"])
        self.assertGreaterEqual(len(form["code_verifier"]), 43)
        self.assertEqual(form["redirect_uri"], query["redirect_uri"])

    def test_an_expired_state_and_an_expired_request_are_refused(self):
        request = self.open()
        _, query = self.approve(request)
        code = self.provider.authorize(query)
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.complete(query["state"], code, session_user=OWNER, now=NOW + oauth.STATE_TTL)
        self.assertIn("expired-state", [d.get("reason") for a, _, d in self.events if a == "oauth.consent-rejected"])
        late = self.open(now=NOW)
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.preview(late["token"], session_user=OWNER, now=NOW + oauth.REQUEST_TTL)
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.start(late["token"], session_user=OWNER, now=NOW + oauth.REQUEST_TTL)
        self.assertEqual(self.provider.calls, [])

    def test_only_the_named_owner_can_view_start_or_complete(self):
        request = self.open()
        for action in (self.store.preview, self.store.start):
            with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
                action(request["token"], session_user="bob@acme.example", now=NOW)
        _, query = self.approve(request)
        code = self.provider.authorize(query)
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.complete(query["state"], code, session_user="bob@acme.example", now=NOW)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW)["state"], "unset")
        # someone else's attempt does not burn the owner's flow
        self.assertEqual(self.store.complete(query["state"], code, session_user=OWNER.upper(), now=NOW)["provider"], "google")
        rejections = [d["reason"] for a, by, d in self.events if a == "oauth.consent-rejected"]
        self.assertEqual(rejections.count("wrong-person"), 3)

    def test_the_provider_declining_spends_the_state_and_is_ledgered(self):
        request = self.open()
        _, query = self.approve(request)
        with self.assertRaisesRegex(oauth.OAuthError, "not granted"):
            self.store.decline(query["state"], "access_denied", session_user=OWNER, now=NOW)
        self.assertIn("oauth.consent-declined", self.actions())
        with self.assertRaisesRegex(oauth.OAuthError, "not valid"):
            self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW)

    def test_scope_widening_or_narrowing_stores_nothing_and_revokes_at_the_provider(self):
        for granted, reason in ((f"{READ_SCOPE} https://www.googleapis.com/auth/gmail.send", "scope-widened"), ("", "scope-narrowed")):
            request = self.open()
            _, query = self.approve(request)
            self.provider.exchange_scope = granted
            before = len(self.provider.revoked)
            with self.assertRaisesRegex(oauth.OAuthError, "different access"):
                self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW)
            self.assertEqual(len(self.provider.revoked), before + 1)
            self.assertEqual(self.store.status("receptionist", "calendar", now=NOW)["state"], "unset")
            self.assertFalse(self.reader.has(STABLE))
            self.assertEqual(next(d for a, _, d in reversed(self.events) if a == "oauth.consent-rejected")["reason"], reason)
        self.assertNotIn("oauth.consent-granted", self.actions())
        # openid/email/profile are benign extras a provider adds on its own
        self.provider.exchange_scope = f"openid email {READ_SCOPE}"
        request = self.open()
        _, query = self.approve(request)
        self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW)

    def test_a_response_without_a_refresh_token_is_refused(self):
        request = self.open()
        _, query = self.approve(request)
        self.provider.omit_refresh = True
        with self.assertRaisesRegex(oauth.OAuthError, "different access"):
            self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW)
        self.assertFalse(self.reader.has(STABLE))

    def test_a_vault_that_refuses_leaves_no_partial_consent(self):
        class Refusing:
            def put(self, ref, value):
                raise RuntimeError("disk full")

            def delete(self, ref):
                pass
        self.store._vault = Refusing()
        request = self.open()
        _, query = self.approve(request)
        with self.assertRaisesRegex(oauth.OAuthError, "could not be stored"):
            self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW)
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW)["state"], "unset")


class RefreshAndRevocationTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.make_grant()
        self.consent()

    def ensure(self, now, **kwargs):
        self.store.ensure_fresh("receptionist", "calendar", read=self.reader, now=now, **kwargs)

    def test_a_fresh_token_is_not_refreshed_and_an_expiring_one_is_refreshed_on_use(self):
        before = self.reader(STABLE)
        calls = len(self.provider.calls)
        resolver = oauth.RefreshingResolver(self.reader, self.store, self.reader, lambda: NOW + 100)
        self.assertEqual(resolver(STABLE), before)
        self.assertEqual(len(self.provider.calls), calls)
        late = oauth.RefreshingResolver(self.reader, self.store, self.reader, lambda: NOW + 3600 - 30)
        after = late(STABLE)
        self.assertNotEqual(after, before)
        self.assertEqual(self.provider.calls[-1][2]["grant_type"], "refresh_token")
        self.assertEqual(late(STABLE), after)  # now fresh again: no second call
        self.assertEqual(sum(1 for call in self.provider.calls if call[2]["grant_type"] == "refresh_token"), 1)
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW + 3600)["state"], "active")
        self.assert_no_leak()

    def test_unrelated_references_pass_straight_through(self):
        self.vault.put("secret://acme-dev/receptionist/other/v1", "plain")
        resolver = oauth.RefreshingResolver(self.reader, self.store, self.reader, lambda: NOW + 99999)
        calls = len(self.provider.calls)
        self.assertEqual(resolver("secret://acme-dev/receptionist/other"), "plain")
        self.assertEqual(len(self.provider.calls), calls)

    def test_a_rotated_refresh_token_replaces_the_old_one_and_old_versions_are_deleted(self):
        self.provider.rotate = True
        first_refresh = self.reader(STABLE.replace(".oauth", ".oauth-refresh"))
        self.ensure(NOW + 3590)
        second_refresh = self.reader(STABLE.replace(".oauth", ".oauth-refresh"))
        self.assertNotEqual(first_refresh, second_refresh)
        self.assertTrue(self.reader.has(STABLE))
        self.assertFalse(self.reader.has(STABLE + "/v1"))
        self.assertFalse(self.reader.has(STABLE.replace(".oauth", ".oauth-refresh") + "/v1"))
        self.ensure(NOW + 7200)
        self.assertIn(self.reader(STABLE.replace(".oauth", ".oauth-refresh")), self.provider.refresh)
        self.assertIn("oauth.refreshed", self.actions())
        self.assert_no_leak()

    def test_revocation_at_the_provider_is_detected_deletes_the_tokens_and_fails_the_grant_with_a_notice(self):
        self.provider.refresh.clear()  # the owner removed access in their Google account
        with self.assertRaisesRegex(oauth.OAuthError, "revoked at the provider"):
            self.ensure(NOW + 3590)
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW + 3590)["state"], "revoked-at-provider")
        self.assertFalse(self.reader.has(STABLE))
        self.assertEqual(self.grant_health(), "failed")
        self.assertIn("oauth.revoked-by-provider", self.actions())
        with self.assertRaisesRegex(oauth.OAuthError, "revoked-at-provider"):
            self.ensure(NOW + 7200)  # no further provider calls
        due = notices.current_notices({}, {}, self.registry.list(), now=NOW + 3600)
        self.assertEqual([n["kind"] for n in due], ["grant-failed"])
        self.assertEqual(due[0]["to"], OWNER)
        self.assert_no_leak()
        # consenting again restores the grant
        request = self.open(now=NOW + 4000)
        _, query = self.approve(request, now=NOW + 4000)
        self.store.complete(query["state"], self.provider.authorize(query), session_user=OWNER, now=NOW + 4000)
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW + 4000)["state"], "active")
        self.assertTrue(self.reader.has(STABLE))
        self.assertEqual(self.grant_health(), "healthy")

    def test_a_transient_provider_failure_degrades_the_grant_and_recovers(self):
        self.provider.refresh_status = (503, "")
        with self.assertRaisesRegex(oauth.OAuthError, "unavailable"):
            self.ensure(NOW + 3590)
        self.assertEqual(self.grant_health(), "degraded")
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW + 3590)["state"], "active")
        self.provider.refresh_status = None
        self.ensure(NOW + 3600)
        self.assertEqual(self.grant_health(), "healthy")

    def test_a_provider_that_widens_scopes_on_refresh_fails_the_connection(self):
        self.provider.refresh_scope = f"{READ_SCOPE} https://mail.google.com/"
        with self.assertRaisesRegex(oauth.OAuthError, "different access"):
            self.ensure(NOW + 3590)
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW)["state"], "failed")
        self.assertEqual(self.grant_health(), "failed")
        self.assertFalse(self.reader.has(STABLE))

    def test_offboarding_revokes_at_the_provider_then_deletes_the_tokens(self):
        refresh = self.reader(STABLE.replace(".oauth", ".oauth-refresh"))
        result = self.store.revoke("receptionist", "calendar", by=OPERATOR, now=NOW + 10, read=self.reader)
        self.assertEqual((result["state"], result["providerRevoked"]), ("revoked", True))
        self.assertEqual(self.provider.revoked, [refresh])
        self.assertFalse(self.reader.has(STABLE))
        self.assertFalse(self.reader.has(STABLE.replace(".oauth", ".oauth-refresh")))
        self.assertEqual(self.grant_health(), "failed")
        self.assertEqual(self.store.status("receptionist", "calendar", now=NOW)["state"], "revoked")
        with self.assertRaisesRegex(oauth.OAuthError, "no active consent"):
            self.store.revoke("receptionist", "calendar", by=OPERATOR, now=NOW + 11)
        with self.assertRaisesRegex(oauth.OAuthError, "revoked"):
            self.ensure(NOW + 99999)

    def test_revoking_without_the_vault_key_deletes_locally_and_says_the_provider_was_not_told(self):
        result = self.store.revoke("receptionist", "calendar", by=OPERATOR, now=NOW + 10)
        self.assertIsNone(result["providerRevoked"])
        self.assertIn("revoke it at the provider", result["note"])
        self.assertEqual(self.provider.revoked, [])
        self.assertFalse(self.reader.has(STABLE))

    def test_revoke_all_covers_one_coworker_and_reports_each_connection(self):
        results = self.store.revoke_all(by=OPERATOR, now=NOW + 10, agent="receptionist", read=self.reader)
        self.assertEqual([(r["agent"], r["state"]) for r in results], [("receptionist", "revoked")])
        self.assertEqual(self.store.revoke_all(by=OPERATOR, now=NOW + 11, agent="receptionist", read=self.reader), [])


class ProviderDeclarationTests(unittest.TestCase):
    def providers(self, **overrides):
        return oauth.load_providers({"idp": {"clientId": "abc-client", "scopesByTool": {"crm.read": ["contacts.read"]},
                                             "authorizeUrl": "https://login.idp.example/authorize", "tokenUrl": "https://login.idp.example/token",
                                             "allowedHosts": ["login.idp.example"], **overrides}})

    def test_presets_are_pinned_and_a_generic_provider_needs_an_exact_host_allow_list(self):
        google = oauth.load_providers(PROVIDERS)["google"]
        self.assertEqual(google["tokenUrl"], TOKEN_URL)
        self.assertEqual(oauth.load_providers({"microsoft": {"preset": "microsoft", "clientId": "11111111-2222",
                                                              "scopesByTool": {"calendar.read": ["Calendars.Read"]}}})["microsoft"]["implicitScopes"], ["offline_access"])
        self.assertEqual(self.providers()["idp"]["revokeUrl"], None)

    def test_hostile_declarations_are_refused(self):
        for overrides, message in (
            ({"authorizeUrl": "http://login.idp.example/authorize"}, "https"),
            ({"tokenUrl": "https://evil.example/token"}, "allow-list"),
            ({"authorizeUrl": "https://login.idp.example/authorize?next=https://evil.example"}, "query"),
            ({"tokenUrl": "https://user:pw@login.idp.example/token"}, "user information"),
            ({"tokenUrl": "https://login.idp.example:8443/token"}, "port"),
            ({"allowedHosts": ["*.idp.example"]}, "fully qualified|exact"),
            ({"allowedHosts": ["10.0.0.5"]}, "public"),
            ({"allowedHosts": ["127.0.0.1"]}, "public"),
            ({"allowedHosts": ["localhost"]}, "."),
            ({"allowedHosts": ["169.254.169.254"]}, "public"),
            ({"revokeUrl": "https://other.idp.example/revoke"}, "allow-list"),
            ({"authParams": {"redirect_uri": "https://evil.example"}}, "authParams"),
            ({"clientId": "x"}, "clientId"),
            ({"scopesByTool": {"crm.read": []}}, "scope"),
            ({"scopesByTool": {"notatool": ["a.b"]}}, "Tool"),
            ({"unknown": 1}, "holds only"),
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(oauth.OAuthError, message):
                self.providers(**overrides)
        with self.assertRaisesRegex(oauth.OAuthError, "pinned"):
            oauth.load_providers({"google": {**PROVIDERS["google"], "tokenUrl": "https://evil.example/token"}})
        with self.assertRaisesRegex(oauth.OAuthError, "preset"):
            oauth.load_providers({"x": {**PROVIDERS["google"], "preset": "nope"}})
        with self.assertRaisesRegex(oauth.OAuthError, "allowedHosts"):
            oauth.load_providers({"x": {"clientId": "abc-client", "scopesByTool": {"crm.read": ["a.b"]}, "authorizeUrl": "https://a.example/x",
                                         "tokenUrl": "https://a.example/y"}})
        with self.assertRaises(oauth.OAuthError):
            oauth.load_providers({})

    def test_the_default_transport_only_talks_to_allow_listed_public_pinned_hosts(self):
        used = []

        class Connection:
            def __init__(self, host, ip):
                used.append((host, ip))

            def request(self, method, path, body=None, headers=None):
                self.path = path

            def getresponse(self):
                class Response:
                    status = 302
                    def read(self, n):  # noqa: E301 - a redirect is returned as is, never followed
                        return b""
                return Response()

            def close(self):
                pass

        transport = oauth.GuardedHttp(["oauth2.googleapis.com"], resolver=lambda host: ["142.250.0.1"], connect=lambda host, ip, timeout: Connection(host, ip))
        self.assertEqual(transport("POST", TOKEN_URL, {}, b"x=1"), (302, b""))
        self.assertEqual(used, [("oauth2.googleapis.com", "142.250.0.1")])
        for url in ("https://evil.example/token", "http://oauth2.googleapis.com/token", "https://oauth2.googleapis.com:8443/token",
                    "https://oauth2.googleapis.com/token?x=1"):
            with self.assertRaisesRegex(oauth.OAuthError, "not allowed"):
                transport("POST", url, {}, b"")
        private = oauth.GuardedHttp(["oauth2.googleapis.com"], resolver=lambda host: ["10.0.0.5"], connect=lambda *a: self.fail("connected"))
        with self.assertRaisesRegex(oauth.OAuthError, "not allowed"):
            private("POST", TOKEN_URL, {}, b"")
        mixed = oauth.GuardedHttp(["oauth2.googleapis.com"], resolver=lambda host: ["142.250.0.1", "169.254.169.254"], connect=lambda *a: self.fail("connected"))
        with self.assertRaisesRegex(oauth.OAuthError, "not allowed"):
            mixed("POST", TOKEN_URL, {}, b"")


def compiled(root: pathlib.Path) -> pathlib.Path:
    path = root / "desired-state.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                    "--agent", "receptionist", "--output", str(path)], check=True, capture_output=True)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["source"]["client"] = "acme"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class GatewayConsentTests(unittest.TestCase):
    config = {
        "publicOrigin": "https://chat.acme.example",
        "directory": {"webchat": {"owner@acme.example": {"identity": "owner@acme.example", "roles": ["approver"]}}},
        "web": {"allowedAddresses": ["owner@acme.example", "bob@acme.example"], "mail": {"from": "chat@acme.example", "host": "smtp.acme.example"}},
        "enrollment": {"vaultPublicKey": KEYS["public"]},
        "oauth": {"providers": PROVIDERS},
    }

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.data = self.root / "data"
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"client": "acme", "environment": "dev", "capabilities": []}))
        self.gateway_json = self.root / "gateway.json"
        self.gateway_json.write_text(json.dumps(self.config))
        self.mail, self.provider = [], FakeProvider()
        self.environ = {"MODEL_GARDEN_WEBCHAT_KEY": "k" * 40, "MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.manifest),
                        "MODEL_GARDEN_OAUTH_GOOGLE_CLIENT_SECRET": CLIENT_SECRET}
        self.gateway = gateway_module.Gateway(
            compiled(self.root), self.data, self.config, self.environ, oauth_http=self.provider,
            mail_sender=lambda to, subject, body: self.mail.append((to, body)), clock=time.time)
        self.reader = vault_module.SealedResolver(self.data / "vault", private_key=KEYS["private"])

    def cli(self, *argv, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, env or {}):
            code = connection_cli.main([str(self.data), *argv, "--manifest", str(self.manifest), "--gateway-config", str(self.gateway_json)])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)

    def open_link(self, **extra):
        code, opened = self.cli("oauth", "open", "--agent", "receptionist", "--connection", "calendar", "--provider", "google",
                                "--tool", "calendar.read", "--owner", OWNER, "--actor", OPERATOR, "--origin", "https://chat.acme.example")
        self.assertEqual(code, 0)
        self.assertEqual(opened["secretRef"], STABLE)
        self.assertEqual(opened["scopes"], [READ_SCOPE])
        return opened["link"].split("#consent=")[1]

    def session_for(self, address, consent):
        self.gateway.handle("POST", "/channels/web/login", {}, json.dumps({"address": address, "oauth": consent}).encode())
        link = self.mail[-1][1].split()[-1]
        self.assertTrue(link.startswith("https://chat.acme.example/connections/oauth/start#login="))
        self.assertTrue(link.endswith("&consent=" + consent))
        login = urllib.parse.unquote(link.split("#login=")[1].split("&")[0])
        return json.loads(self.gateway.handle("POST", "/channels/web/session", {}, json.dumps({"login": login}).encode()).body)["session"]

    def post(self, path, document, session=None):
        headers = {"Authorization": f"Bearer {session}"} if session else {}
        response = self.gateway.handle("POST", path, headers, json.dumps(document).encode())
        return response.status, json.loads(response.body)

    def test_the_owner_signs_in_reviews_scopes_is_redirected_and_the_callback_stores_tokens_only_in_the_vault(self):
        token = self.open_link()
        for path in ("/connections/oauth/start", "/connections/oauth/callback"):
            page = self.gateway.handle("GET", path, {}, b"")
            self.assertEqual(page.status, 200)
            self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
        script = self.gateway.handle("GET", "/connections/oauth/app.js", {}, b"")
        self.assertEqual(script.status, 200)
        self.assertNotIn(b"localStorage", script.body)
        self.assertEqual(self.post("/connections/oauth/start", {"token": token, "action": "preview"})[0], 401)
        bob = self.session_for("bob@acme.example", token)
        self.assertEqual(self.post("/connections/oauth/start", {"token": token, "action": "preview"}, bob)[0], 400)
        self.assertEqual(self.post("/connections/oauth/start", {"token": token, "action": "authorize"}, bob)[0], 400)
        owner = self.session_for(OWNER, token)
        status, preview = self.post("/connections/oauth/start", {"token": token, "action": "preview"}, owner)
        self.assertEqual((status, preview["scopes"], preview["tools"], preview["host"]), (200, [READ_SCOPE], ["calendar.read"], "accounts.google.com"))
        status, started = self.post("/connections/oauth/start", {"token": token, "action": "authorize"}, owner)
        self.assertEqual(status, 200)
        parts, query = query_of(started["redirect"])
        self.assertEqual(parts.netloc, "accounts.google.com")
        code = self.provider.authorize(query)
        # the callback needs the owner's session: the redirect alone proves nothing
        self.assertEqual(self.post("/connections/oauth/callback", {"state": query["state"], "code": code})[0], 401)
        self.assertEqual(self.post("/connections/oauth/callback", {"state": query["state"], "code": code}, bob)[0], 400)
        status, done = self.post("/connections/oauth/callback", {"state": query["state"], "code": code}, owner)
        self.assertEqual((status, done), (200, {"ok": True, "provider": "google", "scopes": [READ_SCOPE]}))
        self.assertTrue(self.reader(STABLE).startswith("access-token-"))
        self.assertEqual(self.post("/connections/oauth/callback", {"state": query["state"], "code": code}, owner)[0], 400)  # replay
        self.assertEqual(self.post("/connections/oauth/callback", {"state": "Z" * 43, "code": code}, owner)[0], 400)  # mismatch
        everything = tree_text(self.data)
        for secret in (*self.provider.leaked, CLIENT_SECRET, code, query["state"], token):
            self.assertNotIn(secret, everything)
        audit = [json.loads(line) for line in (self.data / "audit.jsonl").read_text().splitlines()]
        granted = [event for event in audit if "oauth.consent-granted" in json.dumps(event)]
        self.assertEqual(len(granted), 1)
        self.assertIn(OWNER, json.dumps(granted[0]))
        self.assertIn("owner-session", json.dumps(granted[0]))
        code_, state = self.cli("oauth", "status", "--agent", "receptionist", "--connection", "calendar")
        self.assertEqual((code_, state["state"], state["consentedBy"]), (0, "active", OWNER))
        self.assertNotIn("secret://", json.dumps(state))
        self.assertNotIn("refreshRef", json.dumps(state))

    def test_the_provider_reporting_an_error_on_the_callback_is_recorded_without_storing_anything(self):
        token = self.open_link()
        owner = self.session_for(OWNER, token)
        _, started = self.post("/connections/oauth/start", {"token": token, "action": "authorize"}, owner)
        _, query = query_of(started["redirect"])
        status, body = self.post("/connections/oauth/callback", {"state": query["state"], "error": "access_denied"}, owner)
        self.assertEqual(status, 400)
        self.assertFalse(self.reader.has(STABLE))
        self.assertIn("oauth.consent-declined", (self.data / "audit.jsonl").read_text())

    def test_the_operator_can_revoke_and_offboarding_includes_oauth(self):
        token = self.open_link()
        owner = self.session_for(OWNER, token)
        _, started = self.post("/connections/oauth/start", {"token": token, "action": "authorize"}, owner)
        _, query = query_of(started["redirect"])
        self.post("/connections/oauth/callback", {"state": query["state"], "code": self.provider.authorize(query)}, owner)
        # no network in tests: the CLI's default transport cannot resolve the provider, so it must say the provider was not told
        with mock.patch("socket.getaddrinfo", side_effect=OSError("offline")):
            code, result = self.cli("oauth", "revoke", "--agent", "receptionist", "--connection", "calendar", "--actor", OPERATOR,
                                    env={"MODEL_GARDEN_VAULT_PRIVATE_KEY": KEYS["private"], "MODEL_GARDEN_OAUTH_GOOGLE_CLIENT_SECRET": CLIENT_SECRET})
        self.assertEqual((code, result["state"], result["providerRevoked"]), (0, "revoked", False))
        self.assertIn("NOT revoked at the provider", result["note"])
        self.assertFalse(self.reader.has(STABLE))
        # offboarding with a live consent
        token = self.open_link()
        owner = self.session_for(OWNER, token)
        _, started = self.post("/connections/oauth/start", {"token": token, "action": "authorize"}, owner)
        _, query = query_of(started["redirect"])
        self.post("/connections/oauth/callback", {"state": query["state"], "code": self.provider.authorize(query)}, owner)
        code, off = self.cli("offboard", "--agent", "receptionist", "--actor", OPERATOR)
        self.assertEqual(code, 0)
        self.assertEqual([(item["connectionId"], item["state"], item["providerRevoked"]) for item in off["oauthConsentsRevoked"]], [("calendar", "revoked", None)])
        self.assertFalse(self.reader.has(STABLE))

    def test_configuration_needs_the_vault_the_origin_and_a_client_secret(self):
        no_enrollment = {key: value for key, value in self.config.items() if key != "enrollment"}
        with self.assertRaisesRegex(gateway_module.GatewayConfigError, "sealed vault"):
            gateway_module.load_config(no_enrollment)
        with self.assertRaisesRegex(gateway_module.GatewayConfigError, "pinned"):
            gateway_module.load_config({**self.config, "oauth": {"providers": {"google": {**PROVIDERS["google"], "tokenUrl": "https://evil.example/t"}}}})
        with self.assertRaisesRegex(gateway_module.GatewayConfigError, "providers"):
            gateway_module.load_config({**self.config, "oauth": {"providers": {}}})
        with self.assertRaisesRegex(gateway_module.GatewayConfigError, "MODEL_GARDEN_OAUTH_GOOGLE_CLIENT_SECRET"):
            gateway_module.Gateway(compiled(self.root), self.data, self.config, {k: v for k, v in self.environ.items() if "OAUTH" not in k})
        plain = gateway_module.Gateway(compiled(self.root), self.data, {k: v for k, v in self.config.items() if k != "oauth"}, self.environ,
                                       mail_sender=lambda *a: None, clock=lambda: NOW)
        self.assertEqual(plain.handle("GET", "/connections/oauth/start", {}, b"").status, 404)
        self.assertEqual(plain.handle("POST", "/connections/oauth/callback", {}, b"{}").status, 404)


class SidecarResolverTests(unittest.TestCase):
    def test_the_sidecar_refreshes_a_consented_token_on_use_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            provider = FakeProvider()
            vault = vault_module.SealedVault(root / "vault", public_key=KEYS["public"])
            store = oauth.OAuthConsentStore(
                root / "oauth-consents", tenant=TENANT, vault=vault, providers=oauth.load_providers(PROVIDERS),
                redirect_uri="https://chat.acme.example/connections/oauth/callback", client_secret=lambda name: CLIENT_SECRET, http=provider)
            request = store.request(agent="receptionist", connection_id="calendar", provider="google", tools=["calendar.read"], owner=OWNER, requested_by=OPERATOR, now=int(time.time()))
            _, query = query_of(store.start(request["token"], session_user=OWNER, now=int(time.time()))["redirect"])
            store.complete(query["state"], provider.authorize(query), session_user=OWNER, now=int(time.time()))
            (root / "manifest.json").write_text(json.dumps({"client": "acme", "environment": "dev"}))
            (root / "gateway.json").write_text(json.dumps({"publicOrigin": "https://chat.acme.example", "oauth": {"providers": PROVIDERS}}))
            environ = {"MODEL_GARDEN_VAULT_PRIVATE_KEY": KEYS["private"], "MODEL_GARDEN_VAULT_ROOT": str(root / "vault"),
                       "MODEL_GARDEN_OAUTH_CONFIG": str(root / "gateway.json"), "MODEL_GARDEN_OAUTH_ROOT": str(root),
                       "MODEL_GARDEN_CAPABILITY_MANIFEST": str(root / "manifest.json"), "MODEL_GARDEN_AUDIT_PATH": str(root / "audit.jsonl"),
                       "MODEL_GARDEN_OAUTH_GOOGLE_CLIENT_SECRET": CLIENT_SECRET}
            resolve = upstream_http.secret_resolver(environ)
            self.assertTrue(resolve(STABLE).startswith("access-token-"))  # fresh: no network
            # make the stored token look expired; the sidecar's own transport has no network here, so use fails closed
            document = json.loads(store.path.read_text())
            document["connections"]["receptionist/calendar"]["accessExpiresAt"] = int(time.time()) - 1
            store.path.write_text(json.dumps(document))
            with mock.patch("socket.getaddrinfo", side_effect=OSError("offline")), self.assertRaises(upstream_http.UpstreamTransportError):
                resolve(STABLE)
            self.assertNotIn(CLIENT_SECRET, (root / "audit.jsonl").read_text() if (root / "audit.jsonl").exists() else "")
            # without the OAuth environment the resolver is unchanged
            plain = upstream_http.secret_resolver({k: v for k, v in environ.items() if k != "MODEL_GARDEN_OAUTH_CONFIG"})
            self.assertIsInstance(plain, upstream_http.VaultThenEnvResolver)


class DeploymentTests(unittest.TestCase):
    def render(self, mutate=None, *, gateway_extra=None):
        helper = sys.modules.get("test_deploy_client_docker") or load("test_deploy_client_docker", "tests/test_deploy_client_docker.py")
        case = helper.DeployClientDockerTests("test_secret_resolution_requires_exact_reference_set")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        profile = case._profile(root)
        gateway = {
            "enabled": True, "public_origin": "https://chat.acme.example", "secrets": {"webchat_key_ref": "secret://webchat/key"},
            "web": {"allowed_addresses": ["owner@acme.example"], "mail": {"from": "chat@acme.example", "host": "smtp.acme.example"}},
            "enrollment": {"vault_public_key": KEYS["public"]},
            "oauth": {"providers": {"google": {"preset": "google", "client_id": CLIENT_ID, "client_secret_ref": "secret://oauth/google-client",
                                               "scopes_by_tool": {"calendar.read": [READ_SCOPE]}}}},
        }
        if mutate:
            mutate(gateway)
        upstream = {"mcp": {"enabled": True, "vault_key_ref": "secret://vault/private"}}
        binding = case._binding(root, {"gateway": gateway, "upstream": upstream})
        deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")
        return root, binding

    def test_the_client_secret_goes_to_the_gateway_and_the_sidecar_and_nowhere_in_the_bundle(self):
        root, binding = self.render()
        compose = yaml.safe_load((root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
        name = "MODEL_GARDEN_OAUTH_GOOGLE_CLIENT_SECRET"
        self.assertIn(name, compose["services"]["channel-gateway"]["environment"])
        sidecar = compose["services"]["governed-tools"]["environment"]
        self.assertIn(name, sidecar)
        self.assertTrue(sidecar["MODEL_GARDEN_OAUTH_CONFIG"].endswith("gateway.json"))
        for service, document in compose["services"].items():
            if service not in ("channel-gateway", "governed-tools"):
                self.assertNotIn(name, json.dumps(document))
        config = json.loads((root / "bundle" / "profile" / ".modelgarden" / "gateway.json").read_text(encoding="utf-8"))
        self.assertEqual(config["oauth"]["providers"]["google"]["clientId"], CLIENT_ID)
        self.assertNotIn("secret://", json.dumps(config))
        self.assertTrue((root / "bundle" / "model-garden-platform" / "platform" / "core" / "oauth_consent.py").is_file())
        refs = deployer._collect_secret_refs(json.loads(pathlib.Path(binding).read_text(encoding="utf-8")))
        self.assertIn("secret://oauth/google-client", refs)
        gateway_config, secret_env, _ = deployer._gateway_settings(json.loads(pathlib.Path(binding).read_text(encoding="utf-8")))
        self.assertEqual(secret_env[name], "secret://oauth/google-client")
        self.assertNotIn("oauth", deployer._gateway_settings({"spec": {}})[0] or {})

    def test_invalid_oauth_blocks_are_refused_at_render_time(self):
        def google(**changes):
            def mutate(gateway):
                provider = dict(gateway["oauth"]["providers"]["google"])
                for key, value in changes.items():
                    if value is None:
                        provider.pop(key, None)
                    else:
                        provider[key] = value
                gateway["oauth"] = {"providers": {"google": provider}}
            return mutate

        cases = (
            (google(client_secret_ref=None), "client_secret_ref"),
            (google(client_secret_ref="plain-text"), "client_secret_ref"),
            (google(token_url="https://evil.example/token"), "pinned"),
            (google(preset=None, authorize_url="http://idp.example/a", token_url="https://idp.example/t", allowed_hosts=["idp.example"]), "https"),
            (google(preset=None, authorize_url="https://idp.example/a", token_url="https://idp.example/t", allowed_hosts=["10.1.2.3"]), "public"),
            (google(scopes_by_tool=None), "scopesByTool"),
            (google(bogus=1), "holds only"),
            (lambda gateway: gateway.update(oauth={"providers": {}}), "providers"),
            (lambda gateway: gateway.update(oauth={"providers": {}, "extra": 1}), "providers"),
            (lambda gateway: gateway.pop("enrollment"), "enrollment"),
        )
        for mutate, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.render(mutate)


if __name__ == "__main__":
    unittest.main()
