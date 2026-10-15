"""Credential-free checks for a team of Agents: per-Agent Tool isolation and per-Agent provider access."""
from __future__ import annotations

import importlib.util
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


team = _load("team_under_test", "platform/core/team.py")
grants = _load("grants_for_team", "platform/core/provider_grants.py")
compiler = _load("compiler_for_team", "scripts/compile-workspace.py")
govern = _load("govern_for_team", "scripts/govern-action.py")
Error = team.TeamError
T0 = 1_800_000_000
ADMIN = "operator@platform.example"
BINDING = {
    "environment": "dev",
    "connectors": {
        "kb": {"provider": "text-knowledge", "tier": 0},
        "crm": {"provider": "built-in-crm", "tier": 0},
        "calendar": {"provider": "google", "token_ref": "secret://google/dev"},
    },
}


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.states = compiler.compile_workspace(ROOT / "examples/workspace", None, environment_binding=BINDING)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.registry = grants.GrantRegistry(pathlib.Path(self._tmp.name), tenant="acme-dev")

    def grant(self, **overrides):
        values = dict(agent="receptionist", family="calendar", provider="google", connection_id="calendar-front", isolation="isolated-grant",
                      acting_identity="grant-receptionist-1", secret_ref="secret://google/dev/receptionist",
                      scopes=["calendar.availability", "calendar.book"], owner="owner@client.example", by=ADMIN, now=T0)
        values.update(overrides)
        return self.registry.create(**values)

    def by_name(self, report):
        return {item["agent"]: item for item in report["agents"]}

    # -- the compiler already emits N Agents --------------------------------------------

    def test_both_reference_agents_compile_with_their_own_tools(self):
        self.assertEqual([team.agent_name(state) for state in self.states], ["account-researcher", "receptionist"])
        tools = {team.agent_name(state): team.tool_names(state) for state in self.states}
        self.assertEqual(tools["account-researcher"], ["crm.read"])
        self.assertIn("calendar.book", tools["receptionist"])
        self.assertNotIn("crm.read", tools["receptionist"])

    # -- Tool isolation -----------------------------------------------------------------

    def test_a_call_is_authorised_only_against_the_calling_agents_own_tools(self):
        self.assertTrue(team.authorize_call(self.states, "receptionist", "calendar.book"))
        self.assertTrue(team.authorize_call(self.states, "account-researcher", "crm.read"))
        with self.assertRaisesRegex(Error, "not one of account-researcher's Tools"):
            team.authorize_call(self.states, "account-researcher", "calendar.book")
        with self.assertRaisesRegex(Error, "not one of receptionist's Tools"):
            team.authorize_call(self.states, "receptionist", "crm.read")  # exists in the environment, but not this Agent's
        with self.assertRaisesRegex(Error, "not an Agent in this environment"):
            team.authorize_call(self.states, "coordinator", "crm.read")

    def test_the_same_call_by_two_agents_has_different_request_identities(self):
        # Approvals bind to the request identity, so one Agent's approval can never be replayed by another.
        first, second = (govern.build_action_request(state, "crm.read", {"account": "a-1"}, initiating_user="alice@client.example") for state in self.states)
        self.assertNotEqual(first["requestId"], second["requestId"])
        self.assertEqual({first["agent"], second["agent"]}, {"account-researcher", "receptionist"})

    # -- own provider access ------------------------------------------------------------

    def test_with_several_agents_one_without_its_own_grant_is_blocked_and_nothing_falls_back_to_the_environment_token(self):
        report = team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0 + 1)
        self.assertEqual((report["mode"], report["ready"]), ("multi-agent", False))
        agents = self.by_name(report)
        self.assertTrue(agents["account-researcher"]["ready"])  # only a Tier 0 family: no external grant needed
        self.assertEqual(agents["account-researcher"]["families"]["crm"]["status"], "built-in")
        self.assertFalse(agents["receptionist"]["ready"])
        self.assertEqual(agents["receptionist"]["families"]["calendar"]["status"], "blocked")
        self.assertEqual(team.blocked_reasons(report), ["receptionist: receptionist has no usable calendar grant of its own"])

    def test_an_own_grant_covering_its_tools_makes_the_agent_ready(self):
        self.grant()
        report = team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0 + 1)
        self.assertTrue(report["ready"])
        calendar = self.by_name(report)["receptionist"]["families"]["calendar"]
        self.assertEqual(calendar["status"], "granted")
        self.assertEqual(len(calendar["grantIds"]), 1)

    def test_a_grant_that_does_not_cover_every_tool_still_blocks(self):
        self.grant(scopes=["calendar.availability"])
        report = team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0 + 1)
        self.assertFalse(report["ready"])
        self.assertIn("does not cover calendar.book", team.blocked_reasons(report)[0])

    def test_another_agents_grant_never_satisfies_this_agent(self):
        self.grant(agent="account-researcher", scopes=["calendar.availability", "calendar.book"])
        report = team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0 + 1)
        self.assertFalse(self.by_name(report)["receptionist"]["ready"])

    def test_revoked_expired_or_shared_login_access_blocks_in_a_team(self):
        granted = self.grant(expires_at=T0 + 100)
        self.assertTrue(team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0 + 50)["ready"])
        self.assertFalse(team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0 + 100)["ready"])  # expired
        self.registry.revoke(granted["id"], by=ADMIN, now=T0 + 60)
        self.assertFalse(team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0 + 61)["ready"])
        shared = grants.GrantRegistry(pathlib.Path(self._tmp.name) / "shared", tenant="acme-dev")
        shared.create(agent="receptionist", family="calendar", provider="google", connection_id="calendar-front", isolation="shared-login",
                      acting_identity="one-login@acme.example", secret_ref="secret://google/dev", scopes=["calendar.availability", "calendar.book"],
                      owner="owner@client.example", by=ADMIN, now=T0)
        report = team.plan(self.states, environment_binding=BINDING, registry=shared, now=T0 + 1)
        self.assertFalse(report["ready"])
        self.assertIn("shared login", team.blocked_reasons(report)[0])

    # -- single-Agent workspaces are unchanged ------------------------------------------

    def test_a_single_agent_workspace_is_ready_exactly_as_before(self):
        single = [state for state in self.states if team.agent_name(state) == "receptionist"]
        report = team.plan(single, environment_binding=BINDING, now=T0)
        self.assertEqual((report["mode"], report["ready"]), ("single-agent", True))
        self.assertEqual(report["agents"][0]["families"]["calendar"]["status"], "environment-binding")

    def test_an_unbound_family_blocks_with_a_plain_reason(self):
        report = team.plan(self.states, environment_binding={"environment": "dev", "connectors": {}}, registry=self.registry, now=T0)
        self.assertFalse(report["ready"])
        self.assertTrue(any("not bound" in reason for reason in team.blocked_reasons(report)))

    # -- identity and attribution -------------------------------------------------------

    def test_agent_names_must_be_unique_and_a_team_cannot_be_empty(self):
        with self.assertRaisesRegex(Error, "two Agents are named receptionist"):
            team.plan([self.states[1], self.states[1]], environment_binding=BINDING, now=T0)
        with self.assertRaises(Error):
            team.plan([], environment_binding=BINDING, now=T0)
        with self.assertRaises(Error):
            team.agent_name({"agent": {"metadata": {}}})

    def test_attribution_is_per_agent_in_the_plan(self):
        report = team.plan(self.states, environment_binding=BINDING, registry=self.registry, now=T0)
        self.assertEqual([item["agent"] for item in report["agents"]], ["account-researcher", "receptionist"])
        self.assertEqual(len({item["agent"] for item in report["agents"]}), 2)


if __name__ == "__main__":
    unittest.main()
