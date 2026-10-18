"""Credential-free checks for the connection lifecycle: ledgered health changes, offboarding and inbox alerts."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cli = _load("runtime_connection_under_test", "scripts/runtime-connection.py")
govern = _load("govern_for_connection", "scripts/govern-action.py")
capabilities = _load("capabilities_for_connection", "platform/core/capabilities.py")
compiler = _load("compiler_for_connection", "scripts/compile-workspace.py")
runtime_module = _load("runtime_for_connection", "platform/runtime/governed_mcp.py")
inbox = _load("inbox_for_connection", "platform/core/inbox.py")
operator = _load("operator_for_connection", "scripts/client-operator.py")

BINDING = {"environment": "dev", "connectors": {
    "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://google/dev", "owner": "owner@client.example"},
    "kb": {"provider": "text-knowledge", "tier": 0},
    "crm": {"provider": "built-in-crm", "tier": 0},
}}
WINDOW = {"start": "2026-09-16T10:00:00+08:00", "end": "2026-09-16T10:30:00+08:00"}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.store = self.root / "governance"
        self.store.mkdir()
        desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
        desired["source"]["client"] = "acme"
        (self.root / "desired.json").write_text(json.dumps(desired), encoding="utf-8")
        self.manifest_path = self.root / "manifest.json"
        # Synthetic external connection exercises generic offboarding only; no vendor adapter is implied.
        manifest = capabilities.build_manifest(desired, BINDING)
        external = next(item for item in manifest["capabilities"] if item["family"] == "calendar")
        external.update(status="available", tier=2, provider="fake-approved-mcp")
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(self.store), *argv])
        return code, out.getvalue(), err.getvalue()

    def events(self, action: str | None = None) -> list[dict]:
        path = self.store / "audit.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if action is None or row.get("action") == action]


class OperatorChangeTests(Case):
    def test_setting_health_is_ledgered_with_the_acting_principal_and_no_secrets(self):
        code, _, _ = self.run_cli("set", "calendar", "revoked", "--actor", "jordan@client.example", "--reason", "token withdrawn")
        self.assertEqual(code, 0)
        (event,) = self.events("connection.set")
        self.assertEqual((event["performedBy"], event["details"]), ("jordan@client.example", {"family": "calendar", "health": "revoked", "reason": "token withdrawn"}))
        self.assertEqual(capabilities.read_connection_health(self.store)["calendar"]["health"], "revoked")
        self.assertNotIn("secret://", (self.store / "audit.jsonl").read_text())
        self.assertTrue(govern.verify_audit_chain(self.store / "audit.jsonl")["valid"])

    def test_a_refused_change_leaves_neither_a_record_nor_a_ledger_event(self):
        code, _, err = self.run_cli("set", "no-such-family", "revoked", "--actor", "jordan@client.example")
        self.assertEqual(code, 1)
        self.assertIn("Runtime connection failed", err)
        self.assertEqual(self.events(), [])
        self.assertEqual(capabilities.read_connection_health(self.store), {})


class RuntimeChangeTests(Case):
    def runtime(self):
        return runtime_module.GovernedToolRuntime(
            self.root / "desired.json", self.store / "audit.jsonl", self.store / "approvals",
            environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.manifest_path)},
        )

    def test_a_runtime_completion_cannot_clear_a_revocation_and_writes_no_event_for_it(self):
        self.run_cli("set", "calendar", "revoked", "--actor", "jordan@client.example")
        before = len(self.events())
        self.runtime()._record_health("calendar", "healthy")
        self.assertEqual(len(self.events()), before)
        self.assertEqual(capabilities.read_connection_health(self.store)["calendar"]["health"], "revoked")


class OffboardTests(Case):
    def test_offboarding_revokes_every_bound_external_connection_and_lists_the_manual_steps(self):
        code, out, _ = self.run_cli("offboard", "--actor", "jordan@client.example", "--reason", "contract ended",
                                    "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["revoked"], ["calendar"])  # Tier 0 kb/crm are local data, handled by the manual steps
        steps = " ".join(report["manualSteps"])
        for needed in ("provider", "secrets", "ledger", "contact log", "docker compose down"):
            self.assertIn(needed, steps)
        self.assertEqual(capabilities.read_connection_health(self.store)["calendar"]["health"], "revoked")
        (event,) = self.events("environment.offboard")
        self.assertEqual((event["performedBy"], event["details"]), ("jordan@client.example", {"families": ["calendar"], "reason": "contract ended"}))

    def test_the_manifest_comes_from_the_environment_or_the_command_fails_closed(self):
        code, out, _ = self.run_cli("offboard", "--actor", "jordan@client.example",
                                    env={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.manifest_path)})
        self.assertEqual((code, json.loads(out)["revoked"]), (0, ["calendar"]))
        other = self.root / "other"
        other.mkdir()
        self.store = other
        for env in ({"MODEL_GARDEN_CAPABILITY_MANIFEST": ""}, {"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "missing.json")}):
            code, _, err = self.run_cli("offboard", "--actor", "jordan@client.example", env=env)
            self.assertEqual(code, 1, env)
            self.assertIn("capability manifest", err)
        self.assertEqual(capabilities.read_connection_health(other), {})
        self.assertFalse((other / "audit.jsonl").exists())

    def test_an_actor_is_required_and_a_manifest_with_no_external_connection_revokes_nothing(self):
        with self.assertRaises(SystemExit):
            self.run_cli("offboard", "--manifest", str(self.manifest_path))
        zero = {"environment": "dev", "connectors": {"kb": BINDING["connectors"]["kb"], "crm": BINDING["connectors"]["crm"]}}
        desired = json.loads((self.root / "desired.json").read_text())
        (self.root / "zero.json").write_text(json.dumps(capabilities.build_manifest(desired, zero)))
        code, out, _ = self.run_cli("offboard", "--actor", "jordan@client.example", "--manifest", str(self.root / "zero.json"))
        self.assertEqual((code, json.loads(out)["revoked"]), (0, []))

    def test_the_operator_command_passes_the_actor_and_reason_and_validates_first(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return mock.Mock(returncode=0, stdout="gov1\n", stderr="")
            return mock.Mock(returncode=0, stdout="{}\n", stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operator.runtime_connection("acme-dev", "offboard", actor="jordan@client.example", reason="ended"), 0)
        self.assertEqual(calls[-1][-5:], ["offboard", "--actor", "jordan@client.example", "--reason", "ended"])
        before = len(calls)
        with self.assertRaises(ValueError):
            operator.runtime_connection("acme-dev", "offboard", actor=" ")
        self.assertEqual(len(calls), before)


class InboxTests(Case):
    def collect(self):
        return inbox.collect(approvals=self.store / "approvals", handoffs=self.store / "handoffs", governance=self.store, now=1_790_000_000)

    def test_failed_and_revoked_connections_lead_the_inbox_and_degraded_ones_follow_open_work(self):
        capabilities.record_connection_health(self.store, "calendar", "revoked", actor="jordan@client.example", reason="token withdrawn")
        capabilities.record_connection_health(self.store, "crm", "degraded", actor="runtime:governed-tools", reason="provider-error")
        capabilities.record_connection_health(self.store, "kb", "healthy", actor="runtime:governed-tools")
        result = self.collect()
        self.assertEqual([(i["kind"], i["family"], i["health"]) for i in result["items"]],
                         [("connection", "calendar", "revoked"), ("connection", "crm", "degraded")])
        self.assertEqual(result["counts"]["connections"], 2)
        text = inbox.format_text(result)
        self.assertIn("CONNECTION  calendar is REVOKED (token withdrawn)", text)
        self.assertIn("connection calendar healthy", text)
        self.assertIn("2 connection(s) not healthy", text)

    def test_nothing_secret_or_personal_reaches_the_inbox_and_healthy_runtimes_stay_quiet(self):
        self.assertEqual(self.collect()["items"], [])
        capabilities.record_connection_health(self.store, "calendar", "failed", actor="runtime:governed-tools", reason="provider-authorization")
        rendered = json.dumps(self.collect())
        for private in ("secret://", "owner@client.example", "token_ref"):
            self.assertNotIn(private, rendered)

    def test_connections_are_ordered_ahead_of_overdue_hand_offs_and_approvals(self):
        handoff = _load("handoff_for_connection", "platform/core/handoff.py")
        request = {"requestId": "1" * 64, "tool": "calendar.book", "arguments": {"note": "x"}}
        handoff.create(self.store / "handoffs", request, capability="calendar", reason="unbound",
                       assignee="jordan@client.example", deadline_hours=1, now=1_789_990_000)
        capabilities.record_connection_health(self.store, "calendar", "failed", actor="runtime:governed-tools")
        kinds = [item["kind"] for item in self.collect()["items"]]
        self.assertEqual(kinds, ["connection", "handoff"])


if __name__ == "__main__":
    unittest.main()
