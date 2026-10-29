from __future__ import annotations

import importlib.util
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "preflight-live-mvp.py"
spec = importlib.util.spec_from_file_location("preflight_live_mvp", MODULE_PATH)
preflight = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(preflight)


class LiveMvpPreflightTests(unittest.TestCase):
    def complete_env(self):
        return {
            "OPENAI_API_KEY": "model-secret",
            "LIVEKIT_URL": "wss://livekit.example.test",
            "LIVEKIT_API_KEY": "livekit-key",
            "LIVEKIT_API_SECRET": "livekit-secret",
            "LIVEKIT_SIP_TRUNK_ID": "trunk-id",
        }

    @mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/docker")
    def test_calendar_never_passes_without_upstream_proof(self, _which):
        result = preflight.check(self.complete_env())
        self.assertFalse(result["ready"])
        self.assertIn("approved upstream MCP execution", result["missing"]["calendar"][0])

    @mock.patch.object(preflight.shutil, "which", return_value=None)
    def test_reports_missing_capabilities_without_secret_values(self, _which):
        env = self.complete_env()
        del env["LIVEKIT_API_SECRET"]
        result = preflight.check(env)
        self.assertFalse(result["ready"])
        self.assertIn("approved upstream MCP execution", result["missing"]["calendar"][0])
        self.assertEqual(result["missing"]["voice"], ["LIVEKIT_API_SECRET"])
        self.assertEqual(result["missing"]["runtime"], ["docker"])
        rendered = repr(result)
        self.assertNotIn("model-secret", rendered)
        self.assertNotIn("livekit-key", rendered)

    def test_whitespace_binding_counts_as_missing(self):
        env = self.complete_env()
        env["OPENAI_API_KEY"] = "   "
        result = preflight.check(env, require_docker=False)
        self.assertEqual(result["missing"]["model"], ["OPENAI_API_KEY"])


    def calendar_fixture(self):
        """A synthetic reviewed provider and private per-coworker grant, never live proof."""
        import datetime as dt
        import hashlib
        import json
        import tempfile
        import importlib.util
        root_handle = tempfile.TemporaryDirectory()
        self.addCleanup(root_handle.cleanup)
        root = pathlib.Path(root_handle.name)
        for directory in ("contracts/v1", "platform/connectors", "tests", "assurance/provider-proof"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        adapter = root / "platform/connectors/upstream_calendar.py"
        adapter.write_text("# synthetic governed upstream adapter\\n", encoding="utf-8")
        (root / "tests/test_upstream_calendar.py").write_text(
            "platform/connectors/upstream_calendar.py\\n    def test_upstream(self): pass\\n", encoding="utf-8")
        provider = {
            "id": "upstream-calendar", "name": "Synthetic Calendar", "family": "calendar", "tier": 2,
            "method": "governed-mcp", "status": "live", "issue": 66,
            "tools": ["calendar.availability", "calendar.book"],
            "adapter": "platform/connectors/upstream_calendar.py",
            "proof": "assurance/provider-proof/upstream-calendar.json",
            "scopeByTool": {
                "calendar.availability": ["calendar.read"], "calendar.book": ["calendar.write"],
            },
        }
        (root / "contracts/v1/integrations.json").write_text(json.dumps({
            "registryVersion": "1.0.0", "providers": [provider],
        }), encoding="utf-8")
        (root / "VERSION").write_text("9.9.9\\n", encoding="utf-8")
        proof = {
            "schemaVersion": 1, "provider": "upstream-calendar", "redacted": True,
            "provedAt": "2026-10-01T00:00:00Z", "expiresAt": "2026-12-01T00:00:00Z",
            "adapterSha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
            "scopes": ["calendar.read", "calendar.write"], "recordedBy": "owner@client.example",
            "evidence": ["synthetic-fixture-not-a-provider-receipt"], "limitations": ["offline test only"],
        }
        proof_path = root / provider["proof"]
        proof_path.write_text(json.dumps(proof), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("grants_for_preflight_test", ROOT / "platform/core/provider_grants.py")
        grants = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(grants)
        grant_root = root / "grants"
        registry = grants.GrantRegistry(grant_root, tenant="acme-dev")
        now = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)
        grant = registry.create(
            agent="receptionist", family="calendar", provider="upstream-calendar",
            connection_id="client-calendar", isolation="isolated-grant",
            acting_identity="delegated:receptionist@client.example",
            secret_ref="secret://calendar/acme-dev/receptionist",
            scopes=["calendar.availability", "calendar.book"], owner="owner@client.example",
            by="operator@platform.example", now=int(now.timestamp()) - 100,
        )
        options = {
            "calendar_provider": "upstream-calendar", "evidence_root": root,
            "grant_root": grant_root, "tenant": "acme-dev", "agent": "receptionist",
            "connection_id": "client-calendar", "now": now, "require_docker": False,
        }
        return root, provider, proof_path, registry, grant, options

    def test_a_reviewed_calendar_requires_a_matching_isolated_live_grant(self):
        _, _, _, _, _, options = self.calendar_fixture()
        result = preflight.check(self.complete_env(), **options)
        self.assertTrue(result["ready"], result)
        self.assertNotIn("calendar", result["missing"])
        self.assertNotIn("secret://", repr(result))
        self.assertNotIn("model-secret", repr(result))

    def test_a_live_registry_label_without_current_proof_cannot_unlock_booking(self):
        import json
        _, _, proof_path, _, _, options = self.calendar_fixture()
        proof = json.loads(proof_path.read_text())
        proof["expiresAt"] = "2026-10-02T00:00:00Z"
        proof_path.write_text(json.dumps(proof))
        self.assertIn("calendar", preflight.check(self.complete_env(), **options)["missing"])
        proof_path.unlink()
        self.assertIn("calendar", preflight.check(self.complete_env(), **options)["missing"])

    def test_wrong_family_missing_booking_or_unreviewed_scope_cannot_unlock(self):
        import json
        root, provider, _, _, _, options = self.calendar_fixture()
        registry_path = root / "contracts/v1/integrations.json"
        for changed in (
            {**provider, "family": "crm"},
            {**provider, "tools": ["calendar.availability"]},
            {**provider, "scopeByTool": {"calendar.availability": ["calendar.read"]}},
        ):
            registry_path.write_text(json.dumps({"registryVersion": "1.0.0", "providers": [changed]}))
            self.assertIn("calendar", preflight.check(self.complete_env(), **options)["missing"], changed)

    def test_revoked_wrong_agent_or_insufficient_grant_does_not_unlock(self):
        _, _, _, registry, grant, options = self.calendar_fixture()
        self.assertIn("calendar", preflight.check(self.complete_env(), **{**options, "agent": "researcher"})["missing"])
        self.assertIn("calendar", preflight.check(self.complete_env(), **{**options, "connection_id": "another"})["missing"])
        registry.revoke(grant["id"], by="owner@client.example", now=int(options["now"].timestamp()))
        self.assertIn("calendar", preflight.check(self.complete_env(), **options)["missing"])

    def test_unscoped_or_shared_login_grant_is_not_sufficient(self):
        _, _, _, registry, grant, options = self.calendar_fixture()
        registry.revoke(grant["id"], by="owner@client.example", now=int(options["now"].timestamp()))
        registry.create(
            agent="receptionist", family="calendar", provider="upstream-calendar",
            connection_id="client-calendar", isolation="shared-login",
            acting_identity="shared@client.example", secret_ref="secret://calendar/acme-dev/shared",
            scopes=["calendar.availability"], owner="owner@client.example",
            by="operator@platform.example", now=int(options["now"].timestamp()) - 10,
        )
        self.assertIn("calendar", preflight.check(self.complete_env(), **options)["missing"])

    def test_legacy_token_and_missing_tenant_binding_never_count_as_calendar_proof(self):
        _, _, _, _, _, options = self.calendar_fixture()
        self.assertIn("calendar", preflight.check({**self.complete_env(), "GOOGLE_CALENDAR_TOKEN": "fake"}, require_docker=False)["missing"])
        self.assertIn("calendar", preflight.check(self.complete_env(), **{**options, "tenant": ""})["missing"])
        self.assertIn("calendar", preflight.check(self.complete_env(), **{**options, "grant_root": None})["missing"])


if __name__ == "__main__":
    unittest.main()
