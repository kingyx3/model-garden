"""Credential-free checks for the weekly owner digest (#236) and the worker failure and recovery alerts (#231)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_runtime_worker as base  # noqa: E402

T0 = base.T0
OWNER = "owner@acme.example"
PEOPLE = {OWNER: {"channel": "slack", "address": "D0OWNER001"}}
SECRET = "Hargreaves boiler 12 Acacia Road"


def at(year, month, day, hour=0, minute=0) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc).timestamp())


class DigestCase(base.Case):
    CONFIG = {"digest": {"to": OWNER, "weekday": 0, "hour": 8, "utcOffsetMinutes": 480}, "people": PEOPLE}  # Monday 08:00 at UTC+8 = Monday 00:00 UTC

    def event(self, runtime, when: int, kind: str, outcome: str, **extra) -> None:
        govern = base.worker._module("govern")
        stamp = dt.datetime.fromtimestamp(when, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        govern.append_audit_event(runtime.audit_path, {"schemaVersion": govern.AUDIT_SCHEMA_VERSION, "timestamp": stamp, "event": kind, "outcome": outcome,
                                                      "requestId": "r" * 64, "agent": "receptionist", "tool": "crm.create", "arguments": {"name": SECRET}, **extra})

    def usage(self, runtime, when: int, channel: str, micros: int, tag: str) -> None:
        govern = base.worker._module("govern")
        stamp = dt.datetime.fromtimestamp(when, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        govern.append_audit_event(runtime.audit_path, {"schemaVersion": govern.AUDIT_SCHEMA_VERSION, "timestamp": stamp, "event": "operator-action",
                                                      "outcome": "recorded", "action": "usage.recorded", "performedBy": "runtime:worker",
                                                      "details": {"usageId": tag, "channel": channel, "costMicrosUsd": micros}})

    def notices(self, kind):
        path = self.data / "worker" / "worker-notices.json"
        pending = json.loads(path.read_text())["pending"].values() if path.exists() else []
        return [item for item in pending if item["kind"] == kind]


class DigestTests(DigestCase):
    MONDAY = at(2026, 10, 19)  # a Monday, 00:00 UTC

    def test_emergency_pause_defers_nonessential_weekly_digest_until_resume(self):
        runtime = self.make(self.CONFIG)
        store = base.worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "incident", self.MONDAY)
        self.assertEqual(runtime.run_digest(self.MONDAY + 10), {"skipped": "paused"})
        self.assertEqual(self.notices("digest"), [])
        self.assertFalse((self.data / "worker" / "digest-sent.json").exists())
        store.resume(OWNER, self.MONDAY + 11)
        self.assertEqual(runtime.run_digest(self.MONDAY + 20),
                         {"windowEnd": self.MONDAY, "sent": True})
        self.assertEqual(len(self.notices("digest")), 1)
        self.assertEqual(runtime.run_digest(self.MONDAY + 30), {"skipped": "already-sent"})

    def test_prequeued_digest_waits_for_resume_but_safety_alerts_still_deliver(self):
        sent = []
        runtime = self.make(self.CONFIG, senders={"slack": lambda address, text: sent.append((address, text))})
        runtime.queue.add("digest", OWNER, "Nonessential weekly summary", reference="digest:week")
        runtime.queue.add("worker-failing", OWNER, "Urgent worker alert", reference="alert:worker")
        store = base.worker._module("pause").PauseStore(self.data)
        store.pause(OWNER, "incident", self.MONDAY)
        runtime.deliver_notices(self.MONDAY + 10)
        self.assertEqual([text for _, text in sent], ["Urgent worker alert"])
        self.assertEqual([notice["kind"] for notice in runtime.queue.pending()], ["digest"])
        store.resume(OWNER, self.MONDAY + 11)
        runtime.deliver_notices(self.MONDAY + 20)
        self.assertEqual([text for _, text in sent], ["Urgent worker alert", "Nonessential weekly summary"])
        self.assertEqual(runtime.queue.pending(), [])

    def test_config_is_validated_and_off_by_default(self):
        self.assertEqual(self.make({}).run_digest(self.MONDAY), {"skipped": "no-digest-configured"})
        for bad in ({"to": OWNER, "weekday": 7}, {"to": OWNER, "hour": 24}, {"to": OWNER, "utcOffsetMinutes": 900}, {"to": OWNER, "weekday": True},
                    {"to": "rm -rf"}, {"weekday": 1}, {"to": OWNER, "x": 1}, "weekly"):
            with self.assertRaises(base.worker.WorkerConfigError, msg=str(bad)):
                base.worker.load_config({"digest": bad})
        self.assertEqual(base.worker.load_config({"digest": {"to": OWNER}})["digest"], {"to": OWNER, "weekday": 0, "hour": 8, "utcOffsetMinutes": 0})

    def test_the_digest_counts_the_window_sends_once_and_holds_no_text_or_identity(self):
        runtime = self.make(self.CONFIG)
        self.clock[0] = self.MONDAY + 3600
        inside = self.MONDAY - 3 * 86_400
        for kind, outcome in (("execution", "succeeded"), ("execution", "succeeded"), ("execution", "failed"), ("approval", "pending"),
                              ("approval", "approved"), ("authorization", "deny"), ("authorization", "hand-off")):
            self.event(runtime, inside, kind, outcome)
        self.event(runtime, self.MONDAY - 9 * 86_400, "execution", "succeeded")  # before the window
        self.event(runtime, self.MONDAY + 60, "execution", "succeeded")  # after the window end
        self.usage(runtime, inside, "slack", 1_500_000, "u1")
        self.usage(runtime, inside, "slack", 500_000, "u2")
        self.usage(runtime, inside, "email", 250_000, "u3")
        report = runtime.run_digest(self.MONDAY + 3600)
        self.assertEqual(report, {"windowEnd": self.MONDAY, "sent": True})
        notice = self.notices("digest")[0]
        self.assertEqual(notice["to"], OWNER)
        text = notice["text"]
        self.assertLessEqual(len(text), 600)
        self.assertIn("2 action(s) done, 1 failed", text)
        self.assertIn("approvals 1 asked, 1 approved, 0 rejected; 1 denied, 1 hand-off(s)", text)
        self.assertIn("3 session(s) (slack 2, email 1), about 2.25 USD (estimate)", text)
        self.assertIn("Waiting now: 0 approval(s)", text)
        self.assertIn("2026-10-19", text)
        for private in (SECRET, "Hargreaves", "Acacia", "crm.create"):
            self.assertNotIn(private, text)
        self.assertNotIn(SECRET, json.dumps(self.events()[-1]))
        self.assertEqual(self.events()[-1]["action"], "digest.sent")
        self.assertEqual(runtime.run_digest(self.MONDAY + 7200), {"skipped": "already-sent"})  # once per window, also after a restart
        restarted = self.make(self.CONFIG)
        self.assertEqual(restarted.run_digest(self.MONDAY + 7200), {"skipped": "already-sent"})
        self.assertEqual(len(self.notices("digest")), 1)

    def test_no_data_still_sends_zeros_and_a_missed_window_is_skipped_not_backfilled(self):
        runtime = self.make(self.CONFIG)
        runtime.run_digest(self.MONDAY + 10)
        self.assertIn("0 action(s) done, 0 failed", self.notices("digest")[0]["text"])
        other = self.root / "data2"
        other.mkdir()
        self.data = other  # a client whose worker was down all week: nothing was ever sent from here
        self.assertEqual(self.make(self.CONFIG).run_digest(self.MONDAY + 3 * 86_400), {"skipped": "window-missed"})
        self.assertEqual(self.notices("digest"), [])

    def test_the_offset_sets_the_window_edge_and_the_next_week_sends_again(self):
        runtime = self.make(self.CONFIG)
        # 23:59 UTC on Sunday is 07:59 Monday at UTC+8: before the configured hour, so the previous window's time, long past its grace.
        self.assertEqual(runtime.run_digest(self.MONDAY - 1), {"skipped": "window-missed"})
        self.event(runtime, self.MONDAY - 1, "execution", "succeeded")  # the last second inside the window
        self.event(runtime, self.MONDAY, "execution", "succeeded")  # the end is exclusive
        runtime.run_digest(self.MONDAY + 10)
        self.assertIn("1 action(s) done", self.notices("digest")[0]["text"])
        self.assertEqual(runtime.run_digest(self.MONDAY + 7 * 86_400 + 10), {"windowEnd": self.MONDAY + 7 * 86_400, "sent": True})
        self.assertEqual(len(self.notices("digest")), 2)


class FailureAlertTests(DigestCase):
    CONFIG = {"escalateTo": OWNER, "people": PEOPLE}

    def beat(self, runtime, report):
        self.clock[0] += 60
        runtime._beat(int(self.clock[0]), report)

    def test_a_job_failing_three_passes_alerts_once_and_recovery_is_announced_once(self):
        runtime = self.make(self.CONFIG)
        bad, good = {"mailbox": {"failed": "OSError"}}, {"mailbox": {"skipped": "x"}}
        self.beat(runtime, bad)
        self.beat(runtime, bad)
        self.assertEqual(self.notices("worker-failing"), [])  # two failures are not yet persistent
        self.beat(runtime, bad)
        self.beat(runtime, bad)
        failing = self.notices("worker-failing")
        self.assertEqual(len(failing), 1)
        self.assertIn("mailbox", failing[0]["text"])
        self.assertNotIn("OSError", failing[0]["text"])  # the job name only, never an error detail
        self.beat(runtime, good)
        recovered = self.notices("worker-recovered")
        self.assertEqual((len(recovered), "mailbox" in recovered[0]["text"]), (1, True))
        self.beat(runtime, good)
        self.assertEqual(len(self.notices("worker-recovered")), 1)

    def test_a_restart_while_failing_is_not_reported_as_a_recovery_and_a_job_that_did_not_run_keeps_its_state(self):
        runtime = self.make(self.CONFIG)
        for _ in range(3):
            self.beat(runtime, {"mailbox": {"failed": "OSError"}})
        restarted = self.make(self.CONFIG)  # streaks are forgotten, the saved alert state is not
        self.beat(restarted, {"scheduler": {"runs": 0}})  # another job ran; mailbox did not this pass
        self.assertEqual(self.notices("worker-recovered"), [])
        self.beat(restarted, {"mailbox": {"skipped": "x"}})  # it ran and succeeded
        self.assertEqual(len(self.notices("worker-recovered")), 1)

    def test_with_nobody_to_tell_nothing_is_queued_and_the_tick_still_works(self):
        runtime = self.make({})
        for _ in range(3):
            self.beat(runtime, {"mailbox": {"failed": "OSError"}})
        self.assertEqual(self.notices("worker-failing"), [])
        self.assertIn("scheduler", runtime.tick())


class DigestDeployTests(unittest.TestCase):
    def test_the_deployer_maps_the_binding_setting_and_refuses_unknown_keys(self):
        deployer = base.worker._load("deployer_for_digest", "scripts/deploy-client-docker.py")
        settings = lambda digest: deployer._worker_settings({"spec": {"worker": {"enabled": True, "weekly_digest": digest}}})[0]  # noqa: E731
        self.assertEqual(settings({"to": OWNER, "weekday": 0, "hour": 9, "utc_offset_minutes": 480})["digest"],
                         {"to": OWNER, "weekday": 0, "hour": 9, "utcOffsetMinutes": 480})
        with self.assertRaises(ValueError):
            settings({"to": OWNER, "token": "x"})


if __name__ == "__main__":
    unittest.main()
