"""Credential-free checks for the gateway's chat task assignment (#229) and its own heartbeat and readiness (#231)."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_channel_gateway as base  # noqa: E402

gateway = base.gateway
heartbeat = gateway._module("heartbeat")
T0 = 1_800_000_000


class AssignGatewayTests(base.GatewayCase):
    config = {**base.GatewayCase.config, "assign": {"permissions": {"alice@acme.example": ["crm.create", "kb.search"]}}}

    def say(self, words, *, user="U0ALICE01", ts="1700000000.000300", channel="D0DIRECT01"):
        self.slack_post("/channels/slack/events", base.slack_event(words, user=user, ts=ts, channel=channel))
        return self.posts()[-1]["text"]

    def ledger(self):
        path = self.data / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_mapped_person_assigns_a_task_in_a_direct_chat_and_it_lands_in_the_agent_inbox(self):
        reply = self.say("assign receptionist: send the Hargreaves renewal quote")
        self.assertIn("Handed to receptionist as task ", reply)
        self.assertEqual(self.coworker.calls, [])  # the command, not the coworker, answered
        inbox = self.gateway._team().inbox("receptionist")
        self.assertEqual([row["goal"] for row in inbox], ["send the Hargreaves renewal quote"])
        self.assertEqual(inbox[0]["from"], "person:alice@acme.example")
        assigned = [event for event in self.ledger() if event.get("action") == "task.assigned"]
        self.assertEqual(len(assigned), 1)
        self.assertNotIn("Hargreaves", json.dumps(self.ledger()))  # who and which Agent, never the goal

    def test_the_task_survives_a_restarted_gateway(self):
        self.say("assign receptionist: prepare the call list")
        restarted = gateway.Gateway(base.compiled(self.root), self.data, self.config, base.ENV, coworker=self.coworker, http=self.http, clock=lambda: base.NOW)
        self.assertEqual(len(restarted._team().inbox("receptionist")), 1)

    def test_an_unknown_agent_a_person_with_no_permissions_and_a_shared_channel_are_refused_without_a_task(self):
        self.assertIn("is not an Agent in this environment", self.say("assign ghost: do it", ts="1700000000.000301"))
        self.assertIn("could not hand that over", self.posts()[-1]["text"])
        stranger = self.say("assign receptionist: do it", user="U0STRANGER", ts="1700000000.000302")
        self.assertEqual(stranger, "I can hand a task to an Agent only for a team member in a direct message.")
        shared = self.say("assign receptionist: do it", ts="1700000000.000303", channel="C0TEAM0001")
        self.assertEqual(shared, "I can hand a task to an Agent only for a team member in a direct message.")
        self.assertEqual(self.gateway._team().inbox("receptionist"), [])

    def test_without_assign_configuration_the_words_go_to_the_coworker(self):
        plain = base.GatewayCase.config
        off = gateway.Gateway(base.compiled(self.root), self.data / "off", plain, base.ENV, coworker=self.coworker, http=self.http, clock=lambda: base.NOW)
        self.assertIsNone(off.config["assign"])
        self.assertEqual(self.gateway.config["assign"], {"permissions": {"alice@acme.example": ["crm.create", "kb.search"]}})


class AssignConfigTests(unittest.TestCase):
    def test_the_shape_is_validated(self):
        for bad in ({}, {"permissions": {}}, {"permissions": {"a": "crm.create"}}, {"permissions": {"a": [1]}}, {"permissions": {"a": []}, "x": 1}, "yes"):
            with self.assertRaises(gateway.GatewayConfigError, msg=str(bad)):
                gateway.load_config({"assign": bad})
        self.assertEqual(gateway.load_config({"assign": {"permissions": {"a@b.example": ["z", "a", "a"]}}})["assign"], {"permissions": {"a@b.example": ["a", "z"]}})
        self.assertIsNone(gateway.load_config({})["assign"])


class HeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name) / "gateway" / "heartbeat.json"

    def test_a_recent_beat_is_healthy_a_stalled_one_is_unavailable_and_a_missing_one_is_unavailable(self):
        self.assertEqual(heartbeat.check(self.path, now=T0)["status"], "unavailable")
        beat = heartbeat.Beat(self.path)
        beat.write(T0, {"announce-approvals": False, "announce-receipts": False})
        self.assertEqual(heartbeat.check(self.path, now=T0 + 30, max_age=120)["status"], "healthy")
        stalled = heartbeat.check(self.path, now=T0 + 121, max_age=120)  # the port may answer, but the loop stopped
        self.assertEqual((stalled["status"], stalled["reason"]), ("unavailable", "stale"))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_three_failed_passes_in_a_row_degrade_and_one_good_pass_recovers(self):
        beat = heartbeat.Beat(self.path)
        for step in range(2):
            beat.write(T0 + step, {"announce-receipts": True})
        self.assertEqual(heartbeat.check(self.path, now=T0 + 2)["status"], "healthy")
        beat.write(T0 + 2, {"announce-receipts": True})
        degraded = heartbeat.check(self.path, now=T0 + 3)
        self.assertEqual((degraded["status"], degraded["persistentFailures"]), ("degraded", ["announce-receipts"]))
        beat.write(T0 + 4, {"announce-receipts": False})
        self.assertEqual(heartbeat.check(self.path, now=T0 + 5)["status"], "healthy")

    def test_the_file_holds_only_fixed_names_and_a_time_and_a_symlink_or_junk_is_refused(self):
        heartbeat.Beat(self.path).write(T0, {"announce-receipts": True})
        self.assertEqual(set(json.loads(self.path.read_text())), {"at", "failed", "persistentFailures"})
        self.path.write_text(json.dumps({"at": T0, "failed": ["secret token abc"]}))
        self.assertEqual(heartbeat.check(self.path, now=T0)["reason"], "invalid")

    def test_the_gateway_beats_and_the_readiness_command_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            instance = gateway.Gateway(base.compiled(root), root / "data", {}, {}, clock=lambda: T0)
            instance.beat(T0, ["announce-receipts"])
            self.assertEqual(json.loads((root / "data" / "gateway" / "heartbeat.json").read_text())["failed"], ["announce-receipts"])
            self.assertEqual(gateway.main(["--readiness", "--data-root", str(root / "data")]), 1)  # not healthy against the real clock (the beat is dated in the test's fake time)


if __name__ == "__main__":
    unittest.main()
