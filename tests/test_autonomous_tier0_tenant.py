"""Regression: autonomous Tier 0 writes must not normalize a corrupt compiled tenant (#77, #107, #115).

These tests use synthetic questions and calls only. They exercise the real governed
sidecar's optional unanswered-question write and the voice-to-CRM filing entry point.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GOVERNED = load("autonomous_tenant_governed", "platform/runtime/governed_mcp.py")
FILING = load("autonomous_tenant_filing", "scripts/runtime-file-calls.py")
TENANT = load("autonomous_tenant_identity", "platform/core/tenant_identity.py")


class AutonomousTenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def governed(self, client):
        runtime = object.__new__(GOVERNED.GovernedToolRuntime)
        runtime.desired_state = {"source": {"client": client, "name": "receptionist"}}
        runtime.governance_root = self.root
        runtime.principal_resolver = None
        runtime.clock = lambda: 1_790_000_000
        return runtime

    def test_unanswered_questions_never_create_default_or_normalized_foreign_tenant_state(self):
        for invalid in (None, "", "ACME", "a/../other", "a" * 64, 123):
            with self.subTest(client=invalid):
                self.governed(invalid)._note_unanswered("Can I arrange Saturday collection?", "no-match")
                self.assertFalse((self.root / "unanswered").exists())
        self.governed("acme")._note_unanswered("Can I arrange Saturday collection?", "no-match")
        self.assertTrue((self.root / "unanswered" / "acme.unanswered.json").is_file())
        self.assertFalse((self.root / "unanswered" / "default.unanswered.json").exists())

    def test_voice_timeline_recorder_rejects_untrusted_tenant_before_store_creation(self):
        args = types.SimpleNamespace(conversations_root=self.root / "conversations", audit_path=self.root / "audit.jsonl")
        crm = mock.Mock()
        crm.identities.return_value = []
        for invalid in (None, "", "ACME", "../other", "a" * 64, 123):
            with self.subTest(client=invalid):
                runtime = types.SimpleNamespace(desired_state={
                    "source": {"client": invalid}, "agent": {"metadata": {"name": "receptionist"}}
                })
                with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
                    FILING._recorder(GOVERNED, runtime, crm, args)
                self.assertFalse(args.conversations_root.exists())
        valid = types.SimpleNamespace(desired_state={
            "source": {"client": "acme"}, "agent": {"metadata": {"name": "receptionist"}}
        })
        self.assertEqual(FILING._recorder(GOVERNED, valid, crm, args).counts,
                         {"recorded": 0, "failed": 0, "skipped": 0})

    def test_invalid_tenant_stops_filing_before_even_opening_crm_state(self):
        effects = []

        class FakeRuntime:
            desired_state = {"source": {"client": "ACME"}}

            def crm_store(self):
                effects.append("crm-opened")
                return mock.Mock()

        fake_module = types.SimpleNamespace(
            GOVERN=types.SimpleNamespace(verify_audit_chain=lambda path: {"valid": True}),
            GovernedToolRuntime=lambda *args, **kwargs: FakeRuntime(),
        )
        original_load = FILING._load

        def patched_load(name, path):
            if name == "runtime_file_calls_governed_mcp":
                return fake_module
            return original_load(name, path)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(FILING, "_load", side_effect=patched_load), \
                mock.patch.object(sys, "stdin", io.StringIO("")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = FILING.main([
                "--transcripts", "-", "--desired-state", str(self.root / "desired.json"),
                "--audit-path", str(self.root / "audit.jsonl"),
                "--approval-root", str(self.root / "approvals"),
                "--conversations-root", str(self.root / "conversations"),
            ])
        self.assertEqual(result, 1)
        self.assertEqual(effects, [])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("tenant identity unavailable", err.getvalue())

    def test_shared_compiled_identity_helper_rejects_normalization(self):
        self.assertEqual(TENANT.tenant_from_compiled({"source": {"client": "acme-dev"}}), "acme-dev")
        for bad in (None, {}, {"source": {}}, {"source": {"client": "ACME"}},
                    {"source": {"client": "a/../other"}}, {"source": {"client": 3}}):
            with self.subTest(document=bad):
                with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
                    TENANT.tenant_from_compiled(bad)


if __name__ == "__main__":
    unittest.main()
