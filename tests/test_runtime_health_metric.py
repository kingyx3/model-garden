"""Credential-free adversarial private runtime health monitoring (#231)."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_metric_under_test", ROOT / "scripts/runtime-health-metric.py")
METRIC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = METRIC
SPEC.loader.exec_module(METRIC)


def result(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


class RuntimeMetricTests(unittest.TestCase):
    def test_one_exact_gateway_container_is_probed_over_private_loopback(self):
        with mock.patch.object(METRIC.subprocess, "run", side_effect=[result("a" * 64 + "\n"), result(), result()]) as run:
            self.assertEqual(METRIC.sample("acme-dev"), {"gateway": True, "worker": True})
        self.assertIn("label=com.docker.compose.project=acme-dev", run.call_args_list[0].args[0])
        self.assertIn("label=com.docker.compose.service=gateway", run.call_args_list[0].args[0])
        for i, route in ((1, "gateway"), (2, "worker")):
            cmd = run.call_args_list[i].args[0]
            self.assertEqual(cmd[:4], ["docker", "exec", "a" * 64, "python3"])
            self.assertIn("127.0.0.1:8080/health/" + route, cmd[-1])
            self.assertTrue(run.call_args_list[i].kwargs["check"])

    def test_missing_duplicate_or_invalid_gateway_never_claims_ready(self):
        for ids in ("", "a" * 64 + "\n" + "b" * 64 + "\n", "not-a-container\n"):
            with self.subTest(ids=ids), mock.patch.object(METRIC.subprocess, "run", return_value=result(ids)) as run:
                self.assertEqual(METRIC.sample("acme-dev"), {"gateway": False, "worker": False})
                run.assert_called_once()
        with mock.patch.object(METRIC.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 8)):
            self.assertEqual(METRIC.sample("acme-dev"), {"gateway": False, "worker": False})

    def test_worker_stale_or_failed_is_independent_of_gateway(self):
        with mock.patch.object(METRIC.subprocess, "run", side_effect=[result("a" * 64 + "\n"), result(),
                                                                     subprocess.CalledProcessError(1, ["docker", "exec"])]):
            self.assertEqual(METRIC.sample("acme-dev"), {"gateway": True, "worker": False})
        with mock.patch.object(METRIC.subprocess, "run", side_effect=[result("a" * 64 + "\n"),
                                                                     subprocess.CalledProcessError(1, ["docker", "exec"]), result()]):
            self.assertEqual(METRIC.sample("acme-dev"), {"gateway": False, "worker": True})

    def test_metrics_are_exact_two_boolean_series_without_runtime_payload(self):
        at = datetime(2026, 10, 28, tzinfo=timezone.utc)
        data = METRIC.build_timeseries("sample-project-123", "123", "asia-southeast1-a", "acme", "dev",
                                       {"gateway": True, "worker": False}, at)
        self.assertEqual([x["metric"]["type"] for x in data["timeSeries"]], list(METRIC.METRICS.values()))
        self.assertEqual([x["points"][0]["value"]["doubleValue"] for x in data["timeSeries"]], [1.0, 0.0])
        self.assertNotIn("heartbeat", json.dumps(data).lower())
        for invalid in ({"gateway": True}, {"gateway": True, "worker": 1}, {"gateway": True, "worker": False, "voice": True}):
            with self.assertRaises(ValueError):
                METRIC.build_timeseries("sample-project-123", "123", "asia-southeast1-a", "acme", "dev", invalid, at)

    def test_bad_project_identity_fails_before_docker_or_publish(self):
        with mock.patch.object(METRIC.VOICE, "metadata", return_value="another-project"), \
             mock.patch.object(METRIC, "sample") as sample, mock.patch.object(METRIC.VOICE, "publish_sample") as publish:
            self.assertEqual(METRIC.main(["--project-id", "sample-project-123", "--client", "acme",
                                          "--environment", "dev", "--compose-project", "acme-dev"]), 1)
            sample.assert_not_called()
            publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
