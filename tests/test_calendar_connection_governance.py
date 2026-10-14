from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


oauth = load("calendar_oauth_refresh_tests", ROOT / "platform/connectors/oauth_refresh.py")
mcp = load("calendar_connection_governance_tests", ROOT / "platform/runtime/governed_mcp.py")
compiler = load("calendar_connection_compiler_tests", ROOT / "scripts/compile-workspace.py")
health = load("calendar_connection_health_tests", ROOT / "platform/core/capabilities.py")

START = "2026-10-14T10:00:00+08:00"
END = "2026-10-14T10:30:00+08:00"
TENANT = "12345678-1234-1234-1234-123456789abc"


def secret(provider):
    payload = {
        "type": "oauth2_refresh", "provider": provider, "client_id": "app-id",
        "client_secret": "private-client-secret", "refresh_token": "long-lived-refresh-secret",
    }
    if provider == "microsoft":
        payload["tenant_id"] = TENANT
    return json.dumps(payload)


class CalendarTokenTests(unittest.TestCase):
    def test_google_refresh_token_cached_and_refreshed_before_expiry(self):
        urls = []
        now = [1000.0]

        def fake(url, fields):
            urls.append((url, fields.copy()))
            return 200, {"access_token": "short-lived-bearer", "expires_in": 120}

        cred = oauth.CalendarCredential(secret("google"), "google", transport=fake, clock=lambda: now[0])
        self.assertEqual(cred.token(), "short-lived-bearer")
        self.assertEqual(cred.token(), "short-lived-bearer")
        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0][0], "https://oauth2.googleapis.com/token")
        self.assertEqual(urls[0][1]["grant_type"], "refresh_token")
        now[0] = 1061
        self.assertEqual(cred.token(), "short-lived-bearer")
        self.assertEqual(len(urls), 2)

    def test_microsoft_token_exchange_pinned_to_exact_guid_tenant(self):
        calls = []
        cred = oauth.CalendarCredential(secret("microsoft"), "microsoft",
            transport=lambda url, fields: (calls.append(url) or (200, {"access_token":"ms-token", "expires_in":3600})))
        self.assertEqual(cred.token(), "ms-token")
        self.assertEqual(calls, [f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"])
        with self.assertRaisesRegex(ValueError, "approved tenant"):
            oauth.CalendarCredential(secret("microsoft").replace(TENANT, "common"), "microsoft")

    def test_provider_mismatch_overbroad_payload_and_auth_failure_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "mismatch"):
            oauth.CalendarCredential(secret("microsoft"), "google")
        data = json.loads(secret("google"))
        data["new_authority"] = "*"
        with self.assertRaisesRegex(ValueError, "fields"):
            oauth.CalendarCredential(json.dumps(data), "google")
        cred = oauth.CalendarCredential(secret("google"), "google", transport=lambda *_: (401, {"access_token":"leak"}))
        with self.assertRaisesRegex(RuntimeError, r"refresh failed [(]401[)]"):
            cred.token()
        with self.assertRaisesRegex(ValueError, "credential JSON"):
            oauth.CalendarCredential("{invalid", "google")
        self.assertEqual(oauth.CalendarCredential("legacy-token", "google").token(), "legacy-token")

    def test_oauth_transport_cannot_call_nonapproved_host(self):
        with self.assertRaisesRegex(ValueError, "allow-list"):
            oauth._exchange("https://evil.example/token", {"refresh_token": "do-not-transmit"})
        with self.assertRaisesRegex(ValueError, "allow-list"):
            oauth._exchange("https://login.microsoftonline.com/common/oauth2/v2.0/token", {"refresh_token":"x"})


class CalendarRevocationTests(unittest.TestCase):
    def runtime(self, root, provider, *, secret_value=None, api_transport=None, oauth_transport=None):
        desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
        desired_path = root / "desired.json"
        desired_path.write_text(json.dumps(desired), encoding="utf-8")
        environ = {
            "MODEL_GARDEN_CALENDAR_PROVIDER": provider,
            "MODEL_GARDEN_INITIATING_IDENTITY": "user:owner",
        }
        if provider == "google":
            environ["MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN"] = secret_value or "unused-token"
        else:
            environ["MODEL_GARDEN_MICROSOFT_CALENDAR_TOKEN"] = secret_value or "unused-token"
            environ["MODEL_GARDEN_MICROSOFT_CALENDAR_MAILBOX"] = "staff@example.com"
        return mcp.GovernedToolRuntime(
            desired_path, root / "audit.jsonl", root / "approvals",
            environ=environ, calendar_transport=api_transport,
            oauth_transport=oauth_transport,
        )

    def test_revocation_blocks_calendar_reads_and_writes_for_both_providers(self):
        for provider in ("google", "microsoft"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                api_calls = []
                refresh_calls = []
                runtime = self.runtime(
                    root, provider, secret_value=secret(provider),
                    api_transport=lambda *args: (api_calls.append(args) or (200, {})),
                    oauth_transport=lambda *args: (refresh_calls.append(args) or (200, {"access_token":"x","expires_in":3600})),
                )
                health.record_connection_health(root, "calendar", "revoked",
                                                actor="owner@example.com", reason="client offboarded")
                with self.assertRaisesRegex(PermissionError, "connection revoked"):
                    runtime.calendar_availability(start=START, end=END)
                with self.assertRaisesRegex(PermissionError, "connection revoked"):
                    runtime.calendar_book(start=START, end=END)
                self.assertEqual(api_calls, [])
                self.assertEqual(refresh_calls, [])
                current = health.read_connection_health(root)["calendar"]
                self.assertEqual(current["health"], "revoked")
                events = (root / "audit.jsonl").read_text(encoding="utf-8")
                self.assertEqual(events.count('"outcome":"deny"'), 2)
                self.assertIn("connection-revoked", events)
                self.assertNotIn("private-client-secret", events)
                self.assertNotIn("long-lived-refresh-secret", events)

    def test_failed_auth_blocks_calls_until_explicit_operator_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            calls = []
            runtime = self.runtime(root, "google", api_transport=lambda *args: (
                calls.append(args) or (200, {"calendars": {"primary":{"busy":[]}}})
            ))
            health.record_connection_health(root, "calendar", "failed", actor="runtime:governed-tools")
            with self.assertRaisesRegex(PermissionError, "connection failed"):
                runtime.calendar_availability(start=START, end=END)
            self.assertEqual(calls, [])
            health.record_connection_health(root, "calendar", "healthy", actor="operator", reason="reauthorized")
            got = runtime.calendar_availability(start=START, end=END)
            self.assertEqual(got["status"], "executed")
            self.assertEqual(len(calls), 1)

    def test_inflight_success_cannot_resurrect_revoked_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            def revoke_then_succeed(*args):
                health.record_connection_health(root, "calendar", "revoked", actor="owner", reason="offboarding")
                return (200, {"calendars":{"primary":{"busy":[]}}})
            runtime = self.runtime(root, "google", api_transport=revoke_then_succeed)
            self.assertEqual(runtime.calendar_availability(start=START, end=END)["status"], "executed")
            self.assertEqual(health.read_connection_health(root)["calendar"]["health"], "revoked")
            with self.assertRaisesRegex(PermissionError, "revoked"):
                runtime.calendar_availability(start=START, end=END)

    def test_oauth_refresh_failure_does_not_leak_secrets_and_disables_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            api_calls = []
            runtime = self.runtime(
                root, "google", secret_value=secret("google"),
                oauth_transport=lambda *_: (403, {"error": "sensitive"}),
                api_transport=lambda *args: (api_calls.append(args) or (200, {})),
            )
            with self.assertRaisesRegex(RuntimeError, r"refresh failed [(]403[)]"):
                runtime.calendar_availability(start=START, end=END)
            self.assertEqual(health.read_connection_health(root)["calendar"]["health"], "failed")
            with self.assertRaises(PermissionError):
                runtime.calendar_availability(start=START, end=END)
            self.assertEqual(api_calls, [])
            text = (root / "audit.jsonl").read_text(encoding="utf-8")
            for value in ("private-client-secret", "long-lived-refresh-secret"):
                self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
