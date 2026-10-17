"""Credential-free checks for per-coworker provider grants: isolation, attested resolution and independent revocation."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


grants = _load("provider_grants_under_test", "platform/core/provider_grants.py")
govern = _load("govern_for_grants", "scripts/govern-action.py")
Error = grants.GrantError
T0 = 1_800_000_000
OWNER = "owner@client.example"
ADMIN = "operator@platform.example"
SECRET_A, SECRET_B = "secret://hubspot/acme-prod/receptionist", "secret://hubspot/acme-prod/researcher"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "grants"
        self.events: list[tuple] = []
        self.registry = self.make()

    def make(self, tenant="acme-prod"):
        return grants.GrantRegistry(self.root, tenant=tenant, audit=lambda *event: self.events.append(event))

    def grant(self, registry=None, **overrides):
        values = dict(agent="receptionist", family="crm", provider="hubspot", connection_id="hubspot-main", isolation="own-identity",
                      acting_identity="svc-receptionist@acme.example", secret_ref=SECRET_A, scopes=["crm.create", "crm.log-interaction"],
                      owner=OWNER, by=ADMIN, now=T0)
        values.update(overrides)
        return (registry or self.registry).create(**values)

    def other(self, **overrides):
        values = dict(agent="researcher", connection_id="hubspot-research", acting_identity="svc-researcher@acme.example",
                      secret_ref=SECRET_B, scopes=["crm.read"])
        values.update(overrides)
        return self.grant(**values)

    def resolve(self, agent="receptionist", tool="crm.create", **overrides):
        values = dict(attested_agent=agent, family="crm", tool=tool, originator_permissions=["crm.create", "crm.read", "crm.log-interaction"],
                      enabled_tools=["crm.create", "crm.read", "crm.log-interaction"], now=T0 + 10)
        values.update(overrides)
        return self.registry.resolve(**values)

    # -- the record ---------------------------------------------------------------------

    def test_a_grant_binds_one_coworker_to_its_own_identity_and_hides_the_secret_reference(self):
        shown = self.grant()
        self.assertEqual((shown["agent"], shown["family"], shown["provider"], shown["isolation"], shown["health"]),
                         ("receptionist", "crm", "hubspot", "own-identity", "healthy"))
        self.assertTrue(shown["multiCoworkerSupported"])
        self.assertNotIn("secretRef", shown)
        self.assertNotIn(SECRET_A, json.dumps(self.registry.list()))
        self.assertEqual(shown["secretRefDigest"], grants.digest(SECRET_A))

    def test_invalid_grants_are_refused(self):
        for overrides in ({"agent": "../x"}, {"family": "CRM"}, {"provider": "Hub Spot"}, {"connection_id": ""}, {"isolation": "sharing"},
                          {"acting_identity": ""}, {"secret_ref": "hunter2-actual-token"}, {"secret_ref": "secret://x"}, {"scopes": []},
                          {"scopes": ["not a tool"]}, {"owner": ""}, {"by": " "}, {"expires_at": T0}, {"expires_at": True}):
            with self.subTest(overrides=overrides), self.assertRaises(Error):
                self.grant(**overrides)

    def test_the_same_coworker_cannot_hold_two_grants_on_one_connection(self):
        self.grant()
        with self.assertRaisesRegex(Error, "already has a grant"):
            self.grant(secret_ref="secret://hubspot/acme-prod/receptionist-2")

    # -- no shared credentials ----------------------------------------------------------

    def test_a_secret_reference_or_identity_held_by_another_coworker_is_refused(self):
        self.grant()
        with self.assertRaisesRegex(Error, "secret reference already belongs to another coworker"):
            self.other(secret_ref=SECRET_A)
        with self.assertRaisesRegex(Error, "provider identity already belongs to another coworker"):
            self.other(acting_identity="svc-receptionist@acme.example")
        self.other()  # distinct credential and identity are fine
        self.assertEqual(len(self.registry.list()), 2)

    def test_a_shared_login_is_unsupported_for_several_coworkers_on_one_family(self):
        lone = self.grant(isolation="shared-login")
        self.assertFalse(lone["multiCoworkerSupported"])
        with self.assertRaisesRegex(Error, "shared-login"):
            self.other()  # a second coworker next to a shared login
        fresh = self.make("globex-prod")
        self.grant(fresh, isolation="own-identity")
        with self.assertRaisesRegex(Error, "shared-login"):
            self.other(registry=fresh, isolation="shared-login", secret_ref="secret://hubspot/globex/x", acting_identity="shared@globex.example")

    # -- resolution from the attested caller only ---------------------------------------

    def test_resolution_gives_this_coworker_its_own_reference_and_never_anothers(self):
        self.grant()
        self.other()
        mine = self.resolve()
        theirs = self.resolve(agent="researcher", tool="crm.read")
        self.assertEqual((mine["secretRef"], theirs["secretRef"]), (SECRET_A, SECRET_B))
        self.assertEqual((mine["actingIdentity"], theirs["actingIdentity"]), ("svc-receptionist@acme.example", "svc-researcher@acme.example"))
        self.assertNotEqual(mine["grantId"], theirs["grantId"])

    def test_confused_deputy_a_coworker_cannot_use_another_coworkers_scopes_or_credential(self):
        self.grant()
        self.other()
        with self.assertRaises(Error):  # the researcher asks for the receptionist's write Tool
            self.resolve(agent="researcher", tool="crm.create")
        with self.assertRaises(Error):  # the receptionist asks for the researcher's read Tool
            self.resolve(agent="receptionist", tool="crm.read")
        with self.assertRaisesRegex(Error, "no crm grant of its own"):
            self.resolve(agent="coordinator", tool="crm.read")  # a coordinator has no grant to lend
        for bad in ("", None, "../receptionist"):
            with self.subTest(agent=bad), self.assertRaises(Error):
                self.resolve(agent=bad)

    def test_rights_are_the_intersection_of_requester_grant_and_enabled_tools(self):
        self.grant()
        self.assertTrue(self.resolve()["grantId"])
        with self.assertRaises(Error):  # the requester may not
            self.resolve(originator_permissions=["crm.read"])
        with self.assertRaises(Error):  # the Tool is not enabled for this coworker
            self.resolve(enabled_tools=["crm.log-interaction"])
        with self.assertRaises(Error):  # the grant does not include it
            self.resolve(tool="crm.read")

    def test_wrong_tenant_registry_is_refused(self):
        self.grant()
        other = self.make("globex-prod")
        with self.assertRaises(Error):
            other.resolve(attested_agent="receptionist", family="crm", tool="crm.create",
                          originator_permissions=["crm.create"], enabled_tools=["crm.create"], now=T0 + 1)
        other_path = self.root / "globex-prod.grants.json"
        other_path.write_text((self.root / "acme-prod.grants.json").read_text())
        other_path.chmod(0o600)  # a store file must be private; the point of this check is the wrong tenant, not the mode
        with self.assertRaisesRegex(Error, "another tenant"):
            other.list()
        with self.assertRaises(Error):
            grants.GrantRegistry(self.root, tenant="Bad Tenant")

    # -- independent lifecycle ----------------------------------------------------------

    def test_revoking_one_coworker_leaves_the_other_working(self):
        first, second = self.grant(), self.other()
        self.registry.revoke(first["id"], by=ADMIN, now=T0 + 5)
        with self.assertRaisesRegex(Error, "revoked"):
            self.resolve()
        self.assertEqual(self.resolve(agent="researcher", tool="crm.read")["grantId"], second["id"])
        with self.assertRaisesRegex(Error, "revoked"):
            self.registry.rotate(first["id"], secret_ref="secret://hubspot/acme-prod/new", by=ADMIN, now=T0 + 6)
        with self.assertRaises(Error):
            self.registry.set_health(first["id"], "healthy", by=ADMIN)  # a revoked grant stays failed

    def test_rotation_needs_a_new_unshared_reference_and_only_affects_that_coworker(self):
        first, second = self.grant(), self.other()
        with self.assertRaisesRegex(Error, "already in use"):
            self.registry.rotate(first["id"], secret_ref=SECRET_B, by=ADMIN, now=T0 + 1)
        with self.assertRaisesRegex(Error, "already in use"):
            self.registry.rotate(first["id"], secret_ref=SECRET_A, by=ADMIN, now=T0 + 1)
        rotated = self.registry.rotate(first["id"], secret_ref="secret://hubspot/acme-prod/receptionist-v2", by=ADMIN, now=T0 + 2)
        self.assertEqual(rotated["rotations"], 1)
        self.assertEqual(self.resolve()["secretRef"], "secret://hubspot/acme-prod/receptionist-v2")
        self.assertEqual(self.resolve(agent="researcher", tool="crm.read")["secretRef"], SECRET_B)
        self.assertEqual(second["rotations"], 0)

    def test_rotated_secret_reference_cannot_be_reused_even_after_revocation(self):
        first = self.grant()
        retired = SECRET_A
        fresh = "secret://hubspot/acme-prod/receptionist-v2"
        self.registry.rotate(first["id"], secret_ref=fresh, by=ADMIN, now=T0 + 1)
        with self.assertRaisesRegex(Error, "previously issued"):
            self.grant(connection_id="another", secret_ref=retired)
        with self.assertRaisesRegex(Error, "already in use"):
            self.registry.rotate(first["id"], secret_ref=retired, by=ADMIN, now=T0 + 2)
        self.registry.revoke(first["id"], by=ADMIN, now=T0 + 3)
        with self.assertRaisesRegex(Error, "previously issued"):
            self.grant(connection_id="another", secret_ref=retired)
        with self.assertRaisesRegex(Error, "another coworker"):
            self.other(secret_ref=retired)
        self.assertNotIn(retired, json.dumps(self.registry.list()))

    def test_grant_store_refuses_symlinked_files(self):
        outside = pathlib.Path(self._tmp.name) / "outside"
        outside.write_text("untouched")
        self.root.mkdir(mode=0o700)
        self.registry.path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.registry.list()
        self.assertEqual(outside.read_text(), "untouched")
        self.registry.path.unlink()
        lock = self.root / ".acme-prod.grants.lock"
        lock.unlink(missing_ok=True)  # the earlier list() already created a real lock file; replace it with a symlink
        lock.symlink_to(outside)
        with self.assertRaises(OSError):
            self.grant()
        self.assertEqual(outside.read_text(), "untouched")

    def test_grant_store_rejects_world_readable_and_oversized_data(self):
        self.grant()
        self.registry.path.chmod(0o644)
        with self.assertRaisesRegex(Error, "private regular"):
            self.registry.list()
        self.registry.path.chmod(0o600)
        self.registry.path.write_bytes(b"x" * 2_000_001)
        with self.assertRaisesRegex(Error, "size limit"):
            self.registry.list()

    def test_failed_or_expired_grants_refuse_rather_than_fall_back(self):
        first = self.grant(expires_at=T0 + 100)
        self.assertEqual(self.resolve(now=T0 + 99)["grantId"], first["id"])
        with self.assertRaisesRegex(Error, "expired"):
            self.resolve(now=T0 + 100)
        self.registry.set_health(first["id"], "failed", by=ADMIN)
        with self.assertRaisesRegex(Error, "failed|expired"):
            self.resolve(now=T0 + 10)
        self.registry.set_health(first["id"], "degraded", by=ADMIN)
        self.assertTrue(self.resolve(now=T0 + 10)["degraded"])
        with self.assertRaises(Error):
            self.registry.set_health(first["id"], "great", by=ADMIN)
        with self.assertRaises(Error):
            self.registry.revoke("g-missing", by=ADMIN, now=T0)

    # -- attribution, ledger and upgrade ------------------------------------------------

    def test_receipt_fields_carry_attribution_without_credentials(self):
        self.grant()
        access = self.resolve()
        receipt = grants.receipt_fields(access, initiating_principal="alice@client.example", request_id="r" * 64)
        self.assertEqual((receipt["agent"], receipt["initiatingPrincipal"], receipt["actingIdentity"]),
                         ("receptionist", "alice@client.example", "svc-receptionist@acme.example"))
        self.assertNotIn("secretRef", receipt)
        self.assertNotIn(SECRET_A, json.dumps(receipt))

    def test_changes_are_ledgered_without_the_reference_name(self):
        first = self.grant()
        self.registry.rotate(first["id"], secret_ref="secret://hubspot/acme-prod/receptionist-v2", by=ADMIN, now=T0 + 1)
        self.registry.set_health(first["id"], "degraded", by=ADMIN)
        self.registry.revoke(first["id"], by=ADMIN, now=T0 + 2)
        self.assertEqual([event[0] for event in self.events], ["grant.create", "grant.rotate", "grant.health", "grant.revoke"])
        self.assertTrue(all(event[1] == ADMIN for event in self.events))
        self.assertNotIn("secret://", json.dumps(self.events))
        audit = pathlib.Path(self._tmp.name) / "audit.jsonl"
        for action, by, details in self.events:
            govern.record_operator_action(audit, action, by, details)
        self.assertTrue(govern.verify_audit_chain(audit)["valid"])
        self.assertEqual((self.root / "acme-prod.grants.json").stat().st_mode & 0o777, 0o600)

    def test_a_single_agent_workspace_upgrades_but_several_coworkers_never_reuse_an_environment_token(self):
        binding = {"environment": "prod", "connectors": {
            "calendar": {"provider": "google", "token_ref": "secret://google/acme-prod"},
            "kb": {"provider": "text-knowledge", "tier": 0}, "crm": {"provider": "built-in-crm", "tier": 0},
        }}
        proposals = grants.upgrade_from_environment(binding, ["receptionist"])
        self.assertEqual(len(proposals), 1)
        self.assertEqual((proposals[0]["agent"], proposals[0]["family"], proposals[0]["isolation"], proposals[0]["multiCoworkerSupported"]),
                         ("receptionist", "calendar", "shared-login", False))
        with self.assertRaisesRegex(Error, "refusing to reuse environment-level provider tokens"):
            grants.upgrade_from_environment(binding, ["receptionist", "account-researcher"])
        with self.assertRaises(Error):
            grants.upgrade_from_environment(binding, [])


if __name__ == "__main__":
    unittest.main()
