"""Credential-free checks that an approval is for now: requests and decisions expire, and a late decision never authorises (#114, #117, #82)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_docs_runtime import Case as RuntimeCase, approval_module, runtime_module  # noqa: E402 - shared fixtures

HOUR = 3600
T0 = 1_800_000_000
REQUEST = "ab" * 32
OTHER = "cd" * 32


_deploy_spec = importlib.util.spec_from_file_location("deploy_client_docker_for_approval_ttl", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "deploy-client-docker.py")
deployer = importlib.util.module_from_spec(_deploy_spec)
assert _deploy_spec.loader is not None
_deploy_spec.loader.exec_module(deployer)


def write(store: pathlib.Path, kind: str, request_id: str, record: dict, *, mtime: float | None = None) -> pathlib.Path:
    path = store / kind / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"requestId": request_id, **record}), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TtlTests(unittest.TestCase):
    def test_default_is_a_day_and_the_environment_can_change_it(self):
        self.assertEqual(approval_module.ttl_seconds({}), 24 * HOUR)
        self.assertEqual(approval_module.ttl_seconds({"MODEL_GARDEN_APPROVAL_TTL_HOURS": "2"}), 2 * HOUR)
        self.assertEqual(approval_module.ttl_seconds({"MODEL_GARDEN_APPROVAL_TTL_HOURS": "720"}), 720 * HOUR)

    def test_a_bad_value_fails_clearly_rather_than_never_expiring(self):
        for bad in ("0", "721", "-1", "soon", "1.5"):
            with self.assertRaises(ValueError, msg=bad):
                approval_module.ttl_seconds({"MODEL_GARDEN_APPROVAL_TTL_HOURS": bad})


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = pathlib.Path(self._tmp.name)

    def test_expired_requests_are_not_listed_and_cannot_be_decided(self):
        write(self.store, "pending", REQUEST, {"requestedAt": T0})
        write(self.store, "pending", OTHER, {"requestedAt": T0 + 20 * HOUR})
        now = T0 + 25 * HOUR
        self.assertEqual([item["requestId"] for item in approval_module.pending(self.store, now=now)], [OTHER])
        listed = approval_module.pending(self.store, now=now, include_expired=True)
        self.assertEqual({item["requestId"]: item.get("expired", False) for item in listed}, {REQUEST: True, OTHER: False})
        with self.assertRaisesRegex(ValueError, "expired"):
            approval_module.decide(self.store, REQUEST, approved=True, approver="owner@x.example", now=now)
        self.assertFalse((self.store / "decisions" / f"{REQUEST}.json").exists())
        decision = approval_module.decide(self.store, OTHER, approved=True, approver="owner@x.example", now=now)
        self.assertEqual(decision["decidedAt"], now)

    def test_a_record_without_a_timestamp_is_aged_by_its_file_time(self):
        write(self.store, "pending", REQUEST, {}, mtime=T0)
        self.assertEqual(len(approval_module.pending(self.store, now=T0 + HOUR)), 1)
        self.assertEqual(approval_module.pending(self.store, now=T0 + 25 * HOUR), [])

    def test_sweep_removes_only_expired_requests_and_decisions_and_reports_ids(self):
        write(self.store, "pending", REQUEST, {"requestedAt": T0, "tool": "crm.update", "arguments": {"note": "private"}})
        write(self.store, "pending", OTHER, {"requestedAt": T0 + 24 * HOUR})
        write(self.store, "decisions", REQUEST, {"decidedAt": T0, "approved": True})
        write(self.store, "decisions", OTHER, {"decidedAt": T0 + 24 * HOUR, "approved": True})
        removed = approval_module.sweep(self.store, now=T0 + 25 * HOUR)
        self.assertEqual(sorted((item["kind"], item["requestId"]) for item in removed), [("decision", REQUEST), ("request", REQUEST)])
        self.assertEqual([item.get("tool") for item in removed if item["kind"] == "request"], ["crm.update"])  # a Tool name only, never arguments
        self.assertNotIn("tool", next(item for item in removed if item["kind"] == "decision"))
        self.assertTrue((self.store / "pending" / f"{OTHER}.json").exists())
        self.assertTrue((self.store / "decisions" / f"{OTHER}.json").exists())
        self.assertEqual(approval_module.sweep(self.store, now=T0 + 25 * HOUR), [])

    def test_sweep_skips_unreadable_or_oddly_named_files(self):
        (self.store / "pending").mkdir()
        (self.store / "pending" / "notes.json").write_text("{}", encoding="utf-8")
        (self.store / "pending" / f"{REQUEST}.json").write_text("not json", encoding="utf-8")
        self.assertEqual(approval_module.sweep(self.store, now=T0 + 99 * HOUR), [])
        self.assertEqual(approval_module.sweep(self.store / "missing", now=T0), [])


class BindingTests(unittest.TestCase):
    """`spec.approvals.ttl_hours` reaches every container that reads the approval store, so an operator can set the window without editing compose."""

    def compose(self, hours):
        gateway = ({"slack": {"botUserId": "U0BOT0001"}}, {}, {"bind": "127.0.0.1", "smtp_bind": "127.0.0.1"})
        return yaml.safe_load(deployer._compose(
            "acme-dev", "openai", {"version": "0.21.2", "commit": "a" * 40}, built_in_tools=True, worker=True, gateway=gateway,
            approval_ttl_hours=deployer._approval_ttl_hours({"spec": {"approvals": {"ttl_hours": hours}}}) if hours is not None else None,
        ))

    def test_the_window_is_passed_to_the_sidecar_the_worker_and_the_gateway(self):
        services = self.compose(48)["services"]
        for name in ("governed-tools", "governed-worker", "channel-gateway"):
            self.assertEqual(services[name]["environment"]["MODEL_GARDEN_APPROVAL_TTL_HOURS"], "48", name)
        self.assertNotIn("MODEL_GARDEN_APPROVAL_TTL_HOURS", services["hermes"].get("environment", {}))  # the model's process never sees it

    def test_unset_keeps_the_runtime_default_and_adds_nothing(self):
        for service in self.compose(None)["services"].values():
            self.assertNotIn("MODEL_GARDEN_APPROVAL_TTL_HOURS", service.get("environment", {}))
        self.assertIsNone(deployer._approval_ttl_hours({"spec": {}}))
        self.assertIsNone(deployer._approval_ttl_hours({"spec": {"approvals": {}}}))

    def test_a_bad_value_or_an_unknown_key_stops_the_deploy_instead_of_never_expiring(self):
        for bad in (0, 721, -1, 1.5, "24", True, [24]):
            with self.assertRaisesRegex(ValueError, "ttl_hours"):
                deployer._approval_ttl_hours({"spec": {"approvals": {"ttl_hours": bad}}})
        with self.assertRaisesRegex(ValueError, "only ttl_hours"):
            deployer._approval_ttl_hours({"spec": {"approvals": {"ttl_hours": 24, "skip": True}}})
        with self.assertRaisesRegex(ValueError, "only ttl_hours"):
            deployer._approval_ttl_hours({"spec": {"approvals": "forever"}})
        self.assertEqual(deployer._approval_ttl_hours({"spec": {"approvals": {"ttl_hours": 720}}}), "720")
        self.assertEqual(deployer._approval_ttl_hours({"spec": {"approvals": {"ttl_hours": 1}}}), "1")


class RuntimeTests(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = float(T0)

    def clocked(self, **kwargs):
        runtime = self.runtime(**kwargs)
        runtime.clock = lambda: self.now
        return runtime

    def ask(self, runtime):
        pending = runtime.docs_create(title="Weekly report", content="# Report")
        self.assertEqual(pending["status"], "pending-approval")
        return pending["request"]["requestId"]

    def decide(self, request_id, *, approved=True):
        return approval_module.decide(self.root / "approvals", request_id, approved=approved, approver="owner@client.example", now=self.now)

    def events(self, action):
        return [event for event in self.audit() if event.get("action") == action]

    def test_a_pending_request_records_when_it_was_asked(self):
        runtime = self.clocked()
        request_id = self.ask(runtime)
        record = json.loads((self.root / "approvals" / "pending" / f"{request_id}.json").read_text())
        self.assertEqual(record["requestedAt"], T0)

    def test_asking_again_does_not_extend_a_live_request_but_restarts_an_expired_one(self):
        runtime = self.clocked()
        request_id = self.ask(runtime)
        path = self.root / "approvals" / "pending" / f"{request_id}.json"
        self.now = T0 + 10 * HOUR
        self.ask(runtime)
        self.assertEqual(json.loads(path.read_text())["requestedAt"], T0)
        self.now = T0 + 30 * HOUR
        self.ask(runtime)
        self.assertEqual(json.loads(path.read_text())["requestedAt"], T0 + 30 * HOUR)

    def test_approving_in_time_runs_the_action(self):
        runtime = self.clocked()
        request_id = self.ask(runtime)
        self.now = T0 + 23 * HOUR
        self.decide(request_id)
        self.assertEqual(runtime.docs_create(title="Weekly report", content="# Report")["status"], "executed")

    def test_a_late_approval_is_refused_and_the_action_still_waits(self):
        runtime = self.clocked()
        request_id = self.ask(runtime)
        self.now = T0 + 25 * HOUR
        with self.assertRaisesRegex(ValueError, "expired"):
            self.decide(request_id)
        self.assertEqual(runtime.docs_create(title="Weekly report", content="# Report")["status"], "pending-approval")

    def test_a_stale_decision_never_authorises_a_later_identical_request(self):
        runtime = self.clocked()
        request_id = self.ask(runtime)
        self.decide(request_id)
        self.now = T0 + 48 * HOUR  # nobody asked the sidecar to run it until two days later
        again = runtime.docs_create(title="Weekly report", content="# Report")
        self.assertEqual(again["status"], "pending-approval")
        self.assertEqual(self.store().list(), [])
        self.assertFalse((self.root / "approvals" / "decisions" / f"{request_id}.json").exists())
        (expired,) = self.events("approval.expired")
        self.assertEqual((expired["details"]["requestId"], expired["details"]["kind"]), (request_id, "decision"))
        self.assertGreaterEqual(expired["details"]["ageSeconds"], 48 * HOUR)
        # a fresh decision for the fresh request works
        self.decide(request_id)
        self.assertEqual(runtime.docs_create(title="Weekly report", content="# Report")["status"], "executed")
        self.assertTrue(runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_a_decision_without_a_timestamp_is_aged_by_its_file_time(self):
        runtime = self.runtime()
        request_id = self.ask(runtime)
        path = write(self.root / "approvals", "decisions", request_id, {"approved": True, "approver": "owner@client.example"},
                     mtime=time.time() - 48 * HOUR)  # written long ago by an older release
        self.assertTrue(path.exists())
        self.assertEqual(runtime.docs_create(title="Weekly report", content="# Report")["status"], "pending-approval")
        self.assertEqual(len(self.events("approval.expired")), 1)

    def test_a_bad_ttl_stops_the_sidecar_starting(self):
        self.runtime()  # writes desired.json
        with self.assertRaisesRegex(ValueError, "MODEL_GARDEN_APPROVAL_TTL_HOURS"):
            runtime_module.GovernedToolRuntime(
                self.root / "desired.json", self.root / "audit.jsonl", self.root / "approvals",
                environ={"MODEL_GARDEN_APPROVAL_TTL_HOURS": "never"},
            )

    def test_a_longer_ttl_is_honoured(self):
        runtime = self.clocked()
        runtime.approval_ttl = approval_module.ttl_seconds({"MODEL_GARDEN_APPROVAL_TTL_HOURS": "72"})
        request_id = self.ask(runtime)
        self.decide(request_id)
        self.now = T0 + 48 * HOUR
        self.assertEqual(runtime.docs_create(title="Weekly report", content="# Report")["status"], "executed")


if __name__ == "__main__":
    unittest.main()
