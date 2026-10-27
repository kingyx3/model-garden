"""Liveness and operational readiness are separate for the governed worker (#231).

No client payload, provider credentials, or live infrastructure is needed.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("test_worker_operational_module", ROOT / "platform/runtime/worker.py")
worker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker)

T0 = 1_800_000_000


class WorkerOperationalReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "worker" / "heartbeat.json"

    def write(self, body: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(body), encoding="utf-8")

    def test_rejects_missing_corrupt_stale_future_and_non_numeric_heartbeats(self) -> None:
        self.assertEqual(worker.operational_health(self.path, now=T0)["status"], "unavailable")
        cases = [
            ({"at": T0 - 301, "failed": []}, "stale"),
            ({"at": T0 + 31, "failed": []}, "future"),
            ({"at": True, "failed": []}, "invalid"),
            ({"at": "1800000000", "failed": []}, "invalid"),
            ({"at": T0, "failed": "secrets"}, "invalid"),
            ({"at": T0, "failed": [], "persistentFailures": ["secret=abc!"]}, "invalid"),
        ]
        for source, expected_reason in cases:
            with self.subTest(source=source):
                self.write(source)
                report = worker.operational_health(self.path, now=T0)
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["reason"], expected_reason)
                self.assertFalse(worker.healthy(self.path, now=T0))
        self.path.write_text("{ broken", encoding="utf-8")
        self.assertEqual(worker.operational_health(self.path, now=T0)["status"], "unavailable")
        self.path.write_text("x" * 4097, encoding="utf-8")
        self.assertEqual(worker.operational_health(self.path, now=T0)["status"], "unavailable")
        self.path.unlink()
        self.path.symlink_to(pathlib.Path(self.tmp.name) / "not-a-heartbeat.json")
        self.assertEqual(worker.operational_health(self.path, now=T0)["status"], "unavailable")

    def test_persistent_job_failure_is_visible_but_does_not_kill_liveness(self) -> None:
        runtime = worker.Worker.__new__(worker.Worker)
        runtime.heartbeat = self.path
        runtime.data = self.path.parent.parent
        runtime.config = {"escalateTo": None, "mailbox": None}
        runtime._failure_streaks = {}
        for attempt in (1, 2, 3):
            runtime._beat(T0 + attempt, {"scheduler": {"failed": "RuntimeError"}, "notices": {"sent": 1}})
            body = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(body["failed"], ["scheduler"])
            self.assertEqual(body["persistentFailures"], ["scheduler"] if attempt >= 3 else [])
            status = worker.operational_health(self.path, now=T0 + attempt)
            self.assertEqual(status["status"], "degraded" if attempt >= 3 else "healthy")
            self.assertTrue(worker.healthy(self.path, now=T0 + attempt))
        # On ticks where a long-interval job is not run, its failure remains observable.
        runtime._beat(T0 + 5, {"notices": {"sent": 0}})
        self.assertEqual(worker.operational_health(self.path, now=T0 + 5)["status"], "degraded")
        # A successful attempt clears the condition.
        runtime._beat(T0 + 6, {"scheduler": {"runs": 0}})
        report = worker.operational_health(self.path, now=T0 + 6)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["persistentFailures"], [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_payload_leaks_to_health_output_and_readiness_exits_failure(self) -> None:
        runtime = worker.Worker.__new__(worker.Worker)
        runtime.heartbeat = self.path
        runtime.data = self.path.parent.parent
        runtime.config = {"escalateTo": None, "mailbox": None}
        runtime._failure_streaks = {}
        for index in range(3):
            runtime._beat(T0 + index, {"mailbox": {"failed": "customer secret payload"}})
        body = self.path.read_text(encoding="utf-8")
        self.assertNotIn("customer secret", body)
        with mock.patch.object(worker.time, "time", return_value=T0 + 2):
            self.assertEqual(worker.main(["--readiness", "--data-root", str(self.path.parent.parent)]), 1)
            self.assertEqual(worker.main(["--health", "--data-root", str(self.path.parent.parent)]), 0)


    def restarted(self):
        """A fresh worker process over the same private governance volume."""
        runtime = worker.Worker.__new__(worker.Worker)
        runtime.heartbeat = self.path
        runtime.data = self.path.parent.parent
        runtime.config = {"escalateTo": None, "mailbox": None}
        runtime._failure_streaks = runtime._load_failure_streaks()
        return runtime

    def test_failure_streak_survives_restart_and_skipped_jobs_do_not_clear_it(self):
        first = self.restarted()
        first._beat(T0 + 1, {"scheduler": {"failed": "RuntimeError"}})
        first._beat(T0 + 2, {"scheduler": {"failed": "RuntimeError"}})
        second = self.restarted()
        second._beat(T0 + 3, {"scheduler": {"failed": "RuntimeError"}})
        self.assertEqual(worker.operational_health(self.path, now=T0 + 3)["persistentFailures"], ["scheduler"])
        third = self.restarted()
        third._beat(T0 + 4, {"notices": {"sent": 0}})
        self.assertEqual(worker.operational_health(self.path, now=T0 + 4)["persistentFailures"], ["scheduler"])
        third._beat(T0 + 5, {"scheduler": {"runs": 0}})
        self.assertEqual(worker.operational_health(self.path, now=T0 + 5)["status"], "healthy")
        self.assertEqual(self.restarted()._failure_streaks["scheduler"], 0)

    def test_corrupt_or_linked_failure_history_cannot_be_reset_into_healthy(self):
        first = self.restarted()
        first._beat(T0 + 1, {"scheduler": {"failed": "RuntimeError"}})
        history = self.path.with_name("failure-streaks.json")
        good = history.read_bytes()
        for attack in ("invalid", "symlink", "hardlink", "permissive"):
            with self.subTest(attack=attack):
                history.unlink()
                if attack == "invalid":
                    history.write_text('{"schemaVersion":1,"streaks":{"scheduler":"zero"}}')
                elif attack == "symlink":
                    history.symlink_to(self.path)
                elif attack == "hardlink":
                    history.hardlink_to(self.path)
                else:
                    history.write_bytes(good)
                    history.chmod(0o666)
                with self.assertRaises(worker.WorkerConfigError):
                    self.restarted()
                # No restarted worker can renew the old heartbeat; a hardlink also invalidates its trust check.
        history.unlink()
        history.write_bytes(good)
        history.chmod(0o600)
        self.assertEqual(self.restarted()._failure_streaks["scheduler"], 1)

    def test_heartbeat_publish_never_truncates_predictable_hardlinked_victim(self):
        first = self.restarted()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        victim = self.path.parent / "victim"
        victim.write_text("keep existing content")
        self.path.with_suffix(".tmp").hardlink_to(victim)
        first._beat(T0 + 1, {"scheduler": {"runs": 1}})
        self.assertEqual(victim.read_text(), "keep existing content")
        self.assertEqual(worker.operational_health(self.path, now=T0 + 1)["status"], "healthy")

    def test_alert_queue_failure_is_observable_and_retried_after_restart(self):
        runtime = self.restarted()
        runtime.config["escalateTo"] = "owner@client.example"
        class FullQueue:
            def add(self, *args, **kwargs):
                raise OSError("synthetic full queue")
        runtime.queue = FullQueue()
        for step in range(1, 6):
            runtime._beat(T0 + step, {"scheduler": {"failed": "RuntimeError"}})
        body = json.loads(self.path.read_text())
        self.assertIn("alerts", body["persistentFailures"])
        self.assertIn("scheduler", body["persistentFailures"])
        self.assertEqual(body["failed"], ["alerts", "scheduler"])
        recovered = self.restarted()
        recovered.config["escalateTo"] = "owner@client.example"
        notices = []
        class ReadyQueue:
            def add(self, *args, **kwargs):
                notices.append((args, kwargs))
        recovered.queue = ReadyQueue()
        recovered._beat(T0 + 6, {"scheduler": {"failed": "RuntimeError"}})
        self.assertEqual(len(notices), 1)
        self.assertNotIn("alerts", json.loads(self.path.read_text())["persistentFailures"])
        self.assertIn("scheduler", json.loads(self.path.read_text())["persistentFailures"])


    def test_initialized_history_deletion_fails_closed_and_legacy_heartbeat_migrates(self):
        first = self.restarted()
        first._beat(T0 + 1, {"scheduler": {"failed": "RuntimeError"}})
        history = self.path.with_name("failure-streaks.json")
        history.unlink()
        with self.assertRaises(worker.WorkerConfigError):
            self.restarted()
        # An existing pre-upgrade trusted heartbeat may preserve its already-persistent failures.
        self.path.with_name("failure-streaks.initialized").unlink()
        self.write({"at": T0, "failed": ["scheduler"], "persistentFailures": ["scheduler"]})
        self.assertEqual(self.restarted()._failure_streaks, {"scheduler": 3})

    def test_untrusted_pre_upgrade_heartbeat_is_not_a_clean_first_boot(self):
        self.write({"at": T0, "failed": [], "persistentFailures": []})
        self.path.write_text("{ invalid")
        with self.assertRaises(worker.WorkerConfigError):
            self.restarted()


if __name__ == "__main__":
    unittest.main()
