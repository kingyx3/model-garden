"""Credential-free checks for the monthly model-spend cap, which uses the emergency pause (#238, #232)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_runtime_worker as base  # noqa: E402

T0 = base.T0  # 2027-01-15
OWNER = "owner@acme.example"
CONFIG = {"spend": {"capCents": 1000, "warnAtPercent": [50, 80]}, "escalateTo": OWNER,
          "people": {OWNER: {"channel": "slack", "address": "D0OWNER001"}}}


def month_start(now: int, months: int = 0) -> str:
    moment = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    index = moment.year * 12 + moment.month - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}-10T00:00:00Z"


def later(now: int, months: int) -> int:
    """The 20th of the month ``months`` after ``now``'s, so a test never depends on where T0 falls in the month."""
    moment = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    index = moment.year * 12 + moment.month - 1 + months
    return int(dt.datetime(index // 12, index % 12 + 1, 20, tzinfo=dt.timezone.utc).timestamp())


class SpendCapTests(base.Case):
    def make_runtime(self, config=None):
        return self.make(CONFIG if config is None else config)

    def spend(self, runtime, cents: float, *, months: int = 0, surface="slack") -> None:
        govern = base.worker._module("govern")
        event = {"schemaVersion": govern.AUDIT_SCHEMA_VERSION, "timestamp": month_start(self.clock[0], months), "event": "operator-action",
                 "outcome": "recorded", "action": "usage.recorded", "performedBy": "runtime:worker",
                 "details": {"usageId": f"u{len(runtime.events_seen) if hasattr(runtime, 'events_seen') else cents}{months}{surface}{self.counter}", "costMicrosUsd": int(cents * 10_000)}}
        self.counter += 1
        govern.append_audit_event(runtime.audit_path, event)

    def setUp(self) -> None:
        super().setUp()
        self.counter = 0

    def pause_state(self):
        return base.worker._module("pause").PauseStore(self.data).status()

    def notices(self, kind):
        path = self.data / "worker" / "worker-notices.json"
        pending = json.loads(path.read_text())["pending"].values() if path.exists() else []
        return [item for item in pending if item["kind"] == kind]

    def test_unconfigured_does_nothing_and_the_config_shape_is_validated(self):
        self.assertEqual(self.make_runtime({}).run_spend(T0), {"skipped": "no-spend-cap-configured"})
        for bad in ({"capCents": 0}, {"capCents": True}, {"capCents": "10"}, {"capCents": 100, "warnAtPercent": [100]}, {"capCents": 100, "x": 1},
                    {"capCents": 100, "warnAtPercent": "80"}, "ten"):
            with self.assertRaises(base.worker.WorkerConfigError, msg=str(bad)):
                base.worker.load_config({"spend": bad})
        self.assertEqual(base.worker.load_config({"spend": {"capCents": 5, "warnAtPercent": [80, 50, 80]}})["spend"], {"capCents": 5, "warnAtPercent": [50, 80]})

    def test_warnings_are_sent_once_per_threshold_and_last_months_spend_does_not_count(self):
        runtime = self.make_runtime()
        self.spend(runtime, 900, months=-1)  # last month: ignored
        self.assertEqual(runtime.run_spend(T0)["percent"], 0)
        self.spend(runtime, 520)
        first = runtime.run_spend(T0 + 1)
        self.assertEqual((first["percent"], first["warned"], first["paused"]), (52, 1, False))
        self.assertEqual(runtime.run_spend(T0 + 2)["warned"], 0)  # the same threshold is not repeated
        self.spend(runtime, 300)
        self.assertEqual(runtime.run_spend(T0 + 3)["warned"], 1)  # 82%: the second threshold
        self.assertEqual(len(self.notices("spend-warning")), 2)
        self.assertFalse(self.pause_state()["paused"])

    def test_at_the_cap_the_coworker_is_paused_once_ledgered_and_the_owner_is_told_once(self):
        runtime = self.make_runtime()
        self.spend(runtime, 1000)
        report = runtime.run_spend(T0)
        self.assertTrue(report["paused"])
        state = self.pause_state()
        self.assertEqual((state["paused"], state["cause"], state["by"], state["window"]), (True, "spend-cap", "runtime:worker", dt.datetime.fromtimestamp(T0, dt.timezone.utc).strftime("%Y-%m")))
        self.assertEqual(len([e for e in self.events() if e.get("action") == "spend.cap-reached"]), 1)
        self.assertEqual(len(self.notices("spend-cap")), 1)
        self.assertFalse(runtime.run_spend(T0 + 1)["paused"])  # already paused: nothing more happens
        self.assertEqual(len(self.notices("spend-cap")), 1)

    def test_manual_resume_and_repause_cannot_bypass_a_spend_cap(self):
        runtime = self.make_runtime()
        self.spend(runtime, 1200)
        runtime.run_spend(T0)
        store = base.worker._module("pause").PauseStore(self.data)
        with self.assertRaisesRegex(ValueError, "spend cap"):
            store.resume(OWNER, T0 + 5)
        with self.assertRaisesRegex(ValueError, "spend cap"):
            store.pause(OWNER, "replace the cap with an owner pause", T0 + 6)
        self.assertEqual(self.pause_state()["cause"], "spend-cap")
        self.assertTrue(self.pause_state()["paused"])
        self.assertFalse(runtime.run_spend(T0 + 10)["paused"])  # still paused: no new transition
        self.assertEqual(len(self.notices("spend-cap")), 1)

    def test_only_worker_reconciliation_can_clear_a_cap_after_an_authorized_cap_increase(self):
        runtime = self.make_runtime()
        self.spend(runtime, 1200)
        runtime.run_spend(T0)
        runtime.config["spend"]["capCents"] = 2000
        self.assertFalse(runtime.run_spend(T0 + 10)["paused"])
        self.assertFalse(self.pause_state()["paused"])
        self.assertEqual(len([e for e in self.events() if e.get("action") == "spend.cap-cleared"]), 1)

    def test_a_new_month_under_the_cap_clears_a_cap_pause_but_never_an_owner_pause(self):
        runtime = self.make_runtime()
        self.spend(runtime, 1000)
        runtime.run_spend(T0)
        self.clock[0] = later(T0, 1)
        runtime.run_spend(self.clock[0])
        self.assertFalse(self.pause_state()["paused"])
        self.assertEqual(len([e for e in self.events() if e.get("action") == "spend.cap-cleared"]), 1)
        base.worker._module("pause").PauseStore(self.data).pause(OWNER, "incident", self.clock[0])
        self.clock[0] = later(T0, 2)
        runtime.run_spend(self.clock[0])
        self.assertTrue(self.pause_state()["paused"])  # an owner's pause is theirs to lift

    def test_a_cap_pause_stays_when_the_new_month_is_already_over_the_cap(self):
        runtime = self.make_runtime()
        self.spend(runtime, 1000)
        runtime.run_spend(T0)
        self.clock[0] = later(T0, 1)
        self.spend(runtime, 1100)
        runtime.run_spend(self.clock[0])
        self.assertTrue(self.pause_state()["paused"])

    def test_an_owner_pause_is_not_relabelled_by_the_cap_and_a_missing_ledger_is_zero(self):
        runtime = self.make_runtime()
        self.assertEqual(runtime.run_spend(T0)["spentMicrosUsd"], 0)
        base.worker._module("pause").PauseStore(self.data).pause(OWNER, "incident", T0)
        self.spend(runtime, 2000)
        runtime.run_spend(T0 + 1)
        self.assertNotIn("cause", self.pause_state())
        self.assertEqual(self.pause_state()["by"], OWNER)

    def test_the_pause_it_sets_denies_material_work_at_the_governed_boundary_and_the_job_is_scheduled(self):
        runtime = self.make_runtime()
        self.assertIn("spend", [name for name, _ in runtime.jobs()])
        self.assertEqual(base.worker.DEFAULT_INTERVALS["spend"], 300)
        self.spend(runtime, 1000)
        runtime.run_spend(T0)
        self.assertTrue(base.worker._module("pause").PauseStore(self.data).is_paused())  # the store the sidecar reads at every call


class SpendCapPlumbingTests(unittest.TestCase):
    def test_the_cause_shows_in_status_and_an_owner_pause_has_none(self):
        import tempfile
        pause = base.worker._module("pause")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pause.PauseStore(root).pause("runtime:worker", "monthly spend cap reached", T0, cause="spend-cap", window="2027-01")
            state = pause.PauseStore(root).status()
            self.assertEqual((state["cause"], state["window"]), ("spend-cap", "2027-01"))
            # An owner cannot replace the active metering hold.
            with self.assertRaisesRegex(ValueError, "spend cap"):
                pause.PauseStore(root).pause("alice@acme.example", "incident", T0)
            self.assertEqual(pause.PauseStore(root).status()["cause"], "spend-cap")
            # Once the worker has reconciled the cap, an owner's independent
            # incident pause remains a normal, separately tracked control.
            self.assertTrue(pause.PauseStore(root).clear_spend_cap(T0 + 1, expected_window="2027-01"))
            pause.PauseStore(root).pause("alice@acme.example", "incident", T0 + 2)
            self.assertNotIn("cause", pause.PauseStore(root).status())

    def test_the_deployer_maps_the_binding_setting_and_refuses_unknown_keys(self):
        deployer = base.worker._load("deployer_for_spend", "scripts/deploy-client-docker.py")
        settings = lambda cap: deployer._worker_settings({"spec": {"worker": {"enabled": True, "monthly_spend_cap": cap}}})[0]  # noqa: E731
        self.assertEqual(settings({"cap_cents": 5000, "warn_at_percent": [50]})["spend"], {"capCents": 5000, "warnAtPercent": [50]})
        self.assertEqual(settings({"cap_cents": 5000})["spend"], {"capCents": 5000})
        with self.assertRaises(ValueError):
            settings({"cap_cents": 5000, "token": "x"})


if __name__ == "__main__":
    unittest.main()
