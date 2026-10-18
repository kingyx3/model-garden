"""Multi-Agent Docker bundles: one runtime per Agent, distinct attested identity, opt-in, single-Agent bundles untouched."""
from __future__ import annotations

import filecmp
import importlib.util
import json
import pathlib
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


team = _load("deploy_team_under_test", "scripts/deploy-team-docker.py")
single = _load("deploy_single_for_team_test", "scripts/deploy-client-docker.py")
GATEWAY = {
    "enabled": True, "public_origin": "https://chat.acme.example", "coworker_api_key_ref": "secret://coworker-api/dev",
    "secrets": {"slack_signing_secret_ref": "secret://slack/signing", "slack_bot_token_ref": "secret://slack/bot",
                "webchat_key_ref": "secret://webchat/key"},
    "directory": {"slack": {"T01/U01": {"identity": "alice@acme.example", "roles": ["approver"]}}},
    "slack": {"bot_user_id": "U0BOT0001"},
    "web": {"allowed_addresses": ["alice@acme.example"], "mail": {"from": "chat@acme.example", "host": "smtp.acme.example"}},
}
WORKER = {"enabled": True, "coworker_api_key_ref": "secret://coworker-api/dev"}


class TeamDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def profile(self, name: str, tools=("kb.search",), source_name: str | None = None) -> pathlib.Path:
        profile = self.root / "profiles" / name
        (profile / ".modelgarden").mkdir(parents=True)
        (profile / "skills" / "model-garden" / "faq").mkdir(parents=True)
        (profile / "SOUL.md").write_text(f"# {name}\n", encoding="utf-8")
        (profile / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "openai", "default": "m"}}), encoding="utf-8")
        state = {"schemaVersion": 1, "source": {"name": source_name or name, "client": "acme"},
                 "tools": [{"metadata": {"name": tool}, "spec": {"type": "internal", "risk": "read"}} for tool in tools]}
        (profile / ".modelgarden" / "desired-state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
        (profile / "skills" / "model-garden" / "faq" / "SKILL.md").write_text("---\nname: faq\n---\nAnswer.\n", encoding="utf-8")
        return profile

    def binding(self, **spec) -> pathlib.Path:
        path = self.root / "binding.json"
        body = {"environment": "dev", "runtime": {"profile": "x", "model_credential_ref": "secret://model/dev"}, **spec}
        path.write_text(json.dumps({"apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding", "spec": body}), encoding="utf-8")
        return path

    def render(self, profiles, binding, name="bundle"):
        return team.render_team_bundle(profiles, binding, self.root / name, "acme-dev")

    def test_each_agent_gets_its_own_runtime_identity_volumes_and_network(self):
        profiles = {"front-desk": self.profile("front-desk"), "billing": self.profile("billing", tools=("kb.search", "crm.read"))}
        metadata = self.render(profiles, self.binding(agents=["front-desk", "billing"]))
        compose = yaml.safe_load((self.root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertEqual(sorted(services), ["billing-governed-tools", "billing-hermes", "front-desk-governed-tools", "front-desk-hermes"])
        identities = {name: services[f"{name}-governed-tools"]["environment"]["MODEL_GARDEN_INITIATING_IDENTITY"] for name in profiles}
        self.assertEqual(identities, {"front-desk": "runtime:hermes:front-desk", "billing": "runtime:hermes:billing"})
        self.assertEqual(len(set(identities.values())), 2)
        for name in profiles:
            tools = services[f"{name}-governed-tools"]
            self.assertEqual(tools["environment"]["MODEL_GARDEN_AGENT"], name)
            self.assertEqual(tools["volumes"], [f"{name}-governance-data:/opt/data"])
            self.assertEqual(tools["networks"], {name: {"aliases": ["governed-tools"]}})
            self.assertEqual(services[f"{name}-hermes"]["depends_on"], {f"{name}-governed-tools": {"condition": "service_healthy"}})
            self.assertEqual(services[f"{name}-hermes"]["build"]["context"], f"./agents/{name}")
            self.assertEqual(services[f"{name}-hermes"]["networks"], {name: {"aliases": ["hermes"]}})
        self.assertEqual(sorted(compose["volumes"]), ["billing-governance-data", "billing-hermes-data", "front-desk-governance-data", "front-desk-hermes-data"])
        self.assertEqual(sorted(compose["networks"]), ["billing", "front-desk"])
        # the volumes (and so approvals, hand-offs, memory, ledger) are never shared between Agents
        mounts = [volume for service in services.values() for volume in service["volumes"]]
        self.assertFalse(set(mounts) & {"governance-data:/opt/data", "hermes-data:/opt/data"})
        self.assertEqual([item["agent"] for item in metadata["agents"]], ["billing", "front-desk"])
        self.assertEqual(metadata["kind"], "DockerTeamDeploymentBundle")
        for name in profiles:  # each Agent ships its own compiled state, and the task-graph executor for the operator command
            state = json.loads((self.root / "bundle" / "agents" / name / "profile" / ".modelgarden" / "desired-state.json").read_text())
            self.assertEqual(state["source"]["name"], name)
            self.assertTrue((self.root / "bundle" / "agents" / name / "model-garden-platform" / "scripts" / "runtime-tasks.py").is_file())
            self.assertTrue((self.root / "bundle" / "agents" / name / "model-garden-platform" / "platform" / "core" / "task_graph.py").is_file())

    def test_secrets_stay_out_of_the_bundle(self):
        profiles = {"a": self.profile("a"), "b": self.profile("b")}
        self.render(profiles, self.binding())
        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in (self.root / "bundle").rglob("*")
                         if p.is_file() and "model-garden-platform" not in p.parts)
        self.assertNotRegex(text, r"\bsk-[A-Za-z0-9]{8,}")
        self.assertIn("MODEL_GARDEN_MODEL_CREDENTIAL", text)

    def test_identity_must_match_the_compiled_agent(self):
        with self.assertRaisesRegex(ValueError, "names must match"):
            self.render({"a": self.profile("a"), "b": self.profile("b", source_name="a")}, self.binding())
        self.assertFalse((self.root / "bundle").exists())

    def test_spec_agents_must_match_the_profiles(self):
        profiles = {"a": self.profile("a"), "b": self.profile("b")}
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.render(profiles, self.binding(agents=["a", "c"]))
        with self.assertRaisesRegex(ValueError, "twice"):
            self.render(profiles, self.binding(agents=["a", "a"]))
        with self.assertRaisesRegex(ValueError, "overlay"):
            self.render(profiles, self.binding(agents={"a": {"environment": "prod"}, "b": {}}), name="x")

    def test_team_is_two_or_more_agents_with_safe_names(self):
        with self.assertRaisesRegex(ValueError, "2 to"):
            self.render({"a": self.profile("a")}, self.binding())
        with self.assertRaisesRegex(ValueError, "Agent name"):
            self.render({"Front_Desk": self.profile("Front_Desk"), "b": self.profile("b")}, self.binding(), name="y")

    def test_overlay_runs_the_gateway_for_one_agent_and_shared_gateway_conflicts_on_ports(self):
        profiles = {"front-desk": self.profile("front-desk"), "billing": self.profile("billing")}
        binding = self.binding(worker=WORKER, agents={"front-desk": {"gateway": GATEWAY}, "billing": {}})
        metadata = self.render(profiles, binding)
        compose = yaml.safe_load((self.root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
        self.assertIn("front-desk-channel-gateway", compose["services"])
        self.assertNotIn("billing-channel-gateway", compose["services"])
        self.assertEqual(compose["services"]["front-desk-channel-gateway"]["ports"], ["127.0.0.1:8080:8080"])
        self.assertEqual({item["agent"]: item["channelGateway"] for item in metadata["agents"]}, {"front-desk": True, "billing": False})
        self.assertIn("billing-governed-worker", compose["services"])
        self.assertEqual(compose["services"]["billing-governed-worker"]["volumes"], ["billing-governance-data:/opt/data"])
        with self.assertRaisesRegex(ValueError, "both publish host port"):
            self.render(profiles, self.binding(worker=WORKER, gateway=GATEWAY), name="shared")

    def test_single_agent_bundles_are_unchanged_and_opt_in(self):
        profile = self.profile("solo")
        first, second = self.root / "one", self.root / "two"
        single.render_bundle(profile, self.binding(), first, "acme-dev")
        single.render_bundle(profile, self.binding(), second, "acme-dev")
        comparison = filecmp.dircmp(first, second)
        self.assertEqual((comparison.left_only, comparison.right_only, comparison.diff_files), ([], [], []))
        compose = yaml.safe_load((first / "compose.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("networks", compose)
        self.assertEqual(sorted(compose["services"]), ["governed-tools", "hermes"])
        self.assertEqual(compose["services"]["governed-tools"]["environment"]["MODEL_GARDEN_INITIATING_IDENTITY"], "runtime:hermes")
        self.assertFalse((first / "model-garden-platform" / "platform" / "core" / "task_graph.py").exists())
        # an agents key in the binding does not change what the single-Agent script renders
        with_agents = self.root / "three"
        single.render_bundle(profile, self.binding(agents=["solo"]), with_agents, "acme-dev")
        self.assertEqual((first / "compose.yaml").read_text(), (with_agents / "compose.yaml").read_text())

    def test_cli(self):
        for name in ("a", "b"):
            self.profile(name)
        binding = self.binding()
        import contextlib
        import io
        import sys
        out = io.StringIO()
        argv = ["deploy-team-docker.py", str(self.root / "profiles"), str(binding), str(self.root / "cli"), "--project-name", "acme-dev"]
        old = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(out):
                self.assertEqual(team.main(), 0)
        finally:
            sys.argv = old
        self.assertIn("2 Agents: a, b", out.getvalue())


if __name__ == "__main__":
    unittest.main()
