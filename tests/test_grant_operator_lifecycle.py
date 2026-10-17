"""Operator-only provider grant lifecycle: no secret values, isolated coworkers, auditable changes."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


operator = _load("grant_lifecycle_operator", "scripts/runtime-connection.py")
grants = _load("grant_lifecycle_registry", "platform/core/provider_grants.py")

REF_ONE = "secret://acme/prod/receptionist-v1"
REF_TWO = "secret://acme/prod/receptionist-v2"
REF_OTHER = "secret://acme/prod/accounts-v1"
TENANT = "acme-prod"


class GrantOperatorCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"client": "acme", "environment": "prod"}))

    def call(self, *arguments, tenant=TENANT, manifest=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [str(self.root / "governance"), "grants", *arguments]
        if manifest:
            args += ["--manifest", str(self.manifest)]
        if tenant:
            args += ["--tenant", tenant]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                status = operator.main(args)
            except SystemExit as exc:
                # argparse correctly rejects disallowed isolation values before main returns.
                status = int(exc.code)
        return status, stdout.getvalue(), stderr.getvalue()

    def create(self, *, agent="receptionist", ref=REF_ONE, identity="receptionist@acme.example",
               connection="crm-main", family="crm", provider="hubspot", scopes=("crm.read", "crm.create"),
               extra=(), tenant=TENANT):
        args = [
            "create", "--agent", agent, "--family", family,
            "--provider", provider, "--connection-id", connection, "--isolation", "own-identity",
            "--acting-identity", identity, "--secret-ref", ref,
            "--owner", "owner@acme.example", "--actor", "operator@acme.example",
        ]
        for scope in scopes:
            args += ["--scope", scope]
        return self.call(*args, *extra, tenant=tenant)

    def registry(self):
        return grants.GrantRegistry(self.root / "governance" / "provider-grants", tenant=TENANT)

    def audit(self):
        audit = self.root / "governance" / "audit.jsonl"
        return audit.read_text() if audit.is_file() else ""

    def test_create_returns_redacted_grant_and_audits_no_secret_ref(self):
        status, output, stderr = self.create()
        self.assertEqual(status, 0, stderr)
        record = json.loads(output)["created"]
        self.assertEqual(record["agent"], "receptionist")
        self.assertEqual(record["connectionId"], "crm-main")
        self.assertEqual(record["scopes"], ["crm.create", "crm.read"])
        self.assertTrue(record["multiCoworkerSupported"])
        self.assertNotIn(REF_ONE, output)
        self.assertNotIn("secret://", output + stderr + self.audit())
        self.assertIn("grant.create", self.audit())
        self.assertIn("operator@acme.example", self.audit())
        self.assertEqual(self.registry().list()[0]["id"], record["id"])
        self.assertEqual((self.registry().path.stat().st_mode & 0o777), 0o600)

    def test_separate_coworkers_get_independent_references_and_revocation(self):
        first = json.loads(self.create()[1])["created"]
        refused, _, error = self.create(agent="account-researcher", connection="crm-accounts",
                                        identity="account-researcher@acme.example", ref=REF_ONE)
        self.assertNotEqual(refused, 0)
        self.assertNotIn(REF_ONE, error)
        refused, _, _ = self.create(agent="account-researcher", connection="crm-accounts",
                                   identity="receptionist@acme.example", ref=REF_OTHER)
        self.assertNotEqual(refused, 0)
        accepted, response, _ = self.create(agent="account-researcher", connection="crm-accounts",
                                           identity="account-researcher@acme.example", ref=REF_OTHER)
        self.assertEqual(accepted, 0)
        second = json.loads(response)["created"]
        self.assertNotEqual(first["id"], second["id"])
        stopped, _, _ = self.call("revoke", first["id"], "--actor", "owner@acme.example")
        self.assertEqual(stopped, 0)
        self.assertEqual(len(self.registry().list()), 2)
        access = self.registry().resolve(attested_agent="account-researcher", family="crm",
                                         connection_id="crm-accounts", tool="crm.read",
                                         originator_permissions=["crm.read"], enabled_tools=["crm.read"],
                                         now=int(time.time()))
        self.assertEqual(access["grantId"], second["id"])
        with self.assertRaises(grants.GrantError):
            self.registry().resolve(attested_agent="receptionist", family="crm", connection_id="crm-main",
                                    tool="crm.read", originator_permissions=["crm.read"],
                                    enabled_tools=["crm.read"], now=int(time.time()))

    def test_rotate_and_health_preserve_grant_identity_and_audit(self):
        original = json.loads(self.create()[1])["created"]
        gid = original["id"]
        code, rotated, error = self.call("rotate", gid, "--secret-ref", REF_TWO, "--actor", "operator@acme.example")
        self.assertEqual(code, 0, error)
        updated = json.loads(rotated)["rotated"]
        self.assertEqual(updated["id"], gid)
        self.assertEqual(updated["rotations"], 1)
        self.assertNotIn("secret://", rotated + error + self.audit())
        access = self.registry().resolve(attested_agent="receptionist", family="crm",
                                         connection_id="crm-main", tool="crm.read",
                                         originator_permissions=["crm.read"], enabled_tools=["crm.read"],
                                         now=int(time.time()))
        self.assertEqual(access["secretRef"], REF_TWO)
        code, health, _ = self.call("health", gid, "failed", "--actor", "operator@acme.example")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(health)["updated"]["health"], "failed")
        with self.assertRaises(grants.GrantError):
            self.registry().resolve(attested_agent="receptionist", family="crm",
                                    connection_id="crm-main", tool="crm.read",
                                    originator_permissions=["crm.read"], enabled_tools=["crm.read"],
                                    now=int(time.time()))
        code, _, _ = self.call("health", gid, "healthy", "--actor", "owner@acme.example")
        self.assertEqual(code, 0)
        code, _, _ = self.call("revoke", gid, "--actor", "owner@acme.example")
        self.assertEqual(code, 0)
        code, _, _ = self.call("rotate", gid, "--secret-ref", REF_ONE, "--actor", "owner@acme.example")
        self.assertNotEqual(code, 0)
        self.assertIn("grant.rotate", self.audit())
        self.assertIn("grant.health", self.audit())
        self.assertIn("grant.revoke", self.audit())

    def test_reprovision_after_revocation_requires_new_secret_and_identity(self):
        first = json.loads(self.create()[1])["created"]
        self.assertEqual(self.call("revoke", first["id"], "--actor", "owner@acme.example")[0], 0)
        old, _, _ = self.create()
        self.assertNotEqual(old, 0)
        renewed, output, error = self.create(ref=REF_TWO, identity="receptionist-v2@acme.example")
        self.assertEqual(renewed, 0, error)
        new_id = json.loads(output)["created"]["id"]
        self.assertNotEqual(first["id"], new_id)
        active = self.registry().resolve(attested_agent="receptionist", family="crm",
                                         connection_id="crm-main", tool="crm.read",
                                         originator_permissions=["crm.read"], enabled_tools=["crm.read"],
                                         now=int(time.time()))
        self.assertEqual(active["grantId"], new_id)

    def test_no_unknown_cross_family_or_arbitrary_scopes(self):
        for kwargs in ({"scopes": ("files.read",)}, {"scopes": ("crm.delete-all",)},
                       {"scopes": ("crm.create", "calendar.book")},
                       {"scopes": ("mcp.vendor.raw",)}):
            code, _, _ = self.create(**kwargs)
            self.assertNotEqual(code, 0)
        self.assertFalse(self.registry().path.exists())

    def test_raw_credentials_expiry_and_shared_login_fail_closed(self):
        code, _, err = self.create(ref="Bearer actual-secret-material")
        self.assertNotEqual(code, 0)
        self.assertNotIn("actual-secret-material", err)
        code, _, err = self.create(extra=("--expires-at", str(int(time.time()) - 50)))
        self.assertNotEqual(code, 0)
        code, _, err = self.call("create", "--agent", "receptionist", "--family", "crm",
            "--provider", "hubspot", "--connection-id", "crm-main", "--isolation", "shared-login",
            "--acting-identity", "shared@acme.example", "--secret-ref", REF_ONE, "--scope", "crm.read",
            "--owner", "owner@acme.example", "--actor", "operator@acme.example")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.registry().path.exists())

    def test_tenant_must_match_manifest_and_create_is_idempotent_only_by_refusal(self):
        code, _, err = self.create(tenant="other-prod")
        self.assertNotEqual(code, 0)
        self.assertNotIn("secret://", err)
        self.assertFalse(self.registry().path.exists())
        code, _, _ = self.create()
        self.assertEqual(code, 0)
        code, _, _ = self.create(ref=REF_TWO)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.registry().list()), 1)

    def test_no_implicit_registry_is_created_for_list_and_revoke(self):
        code, listed, err = self.call("list")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(listed)["registry"], "none")
        self.assertFalse(self.registry().path.exists())
        code, _, err = self.call("revoke", "g-0123456789ab", "--actor", "owner@acme.example")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.registry().path.exists())

    def test_symlink_registry_is_refused(self):
        state = self.root / "outside.json"
        state.write_text('{"secret":"never touch"}')
        folder = self.root / "governance" / "provider-grants"
        folder.mkdir(parents=True)
        (folder / f"{TENANT}.grants.json").symlink_to(state)
        code, output, err = self.create()
        self.assertNotEqual(code, 0)
        self.assertNotIn("never touch", output + err)
        self.assertEqual(state.read_text(), '{"secret":"never touch"}')


if __name__ == "__main__":
    unittest.main()
