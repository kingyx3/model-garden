"""Offline adversarial coverage for private GCE voice health publication (#231)."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("voice_metric_under_test", ROOT / "scripts" / "voice-health-metric.py")
METRIC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = METRIC
SPEC.loader.exec_module(METRIC)


def result(text):
    return subprocess.CompletedProcess([], 0, text, "")


class VoiceMetricTests(unittest.TestCase):
    def test_only_one_exact_project_voice_container_can_report_healthy(self):
        with mock.patch.object(METRIC.subprocess, "run", side_effect=[
            result("a1\n"), result(json.dumps({"Running": True, "Health": {"Status": "healthy", "Log": [{"End": datetime.now(timezone.utc).isoformat(), "ExitCode": 0}]}}))
        ]) as run:
            self.assertTrue(METRIC.docker_voice_ready("client-dev"))
        self.assertIn("label=com.docker.compose.project=client-dev", run.call_args_list[0].args[0])
        self.assertIn("label=com.docker.compose.service=voice", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0][-1], "a1")

    def test_missing_duplicate_unhealthy_or_untrusted_docker_state_never_reports_ready(self):
        cases = [
            [result("")], [result("a1\na2\n")],
            [result("a1\n"), result('{"Running":true,"Health":{"Status":"starting"}}')],
            [result("a1\n"), result('{"Running":false,"Health":{"Status":"healthy"}}')],
            [result("a1\n"), result('{"Running":true}')],
            [result("a1\n"), result('{"Running":true,"Health":{"Status":"unhealthy"}}')],
            [result("a1\n"), result('not-json')],
        ]
        for responses in cases:
            with self.subTest(responses=responses), mock.patch.object(METRIC.subprocess, "run", side_effect=responses):
                self.assertFalse(METRIC.docker_voice_ready("client-dev"))
        with mock.patch.object(METRIC.subprocess, "run", side_effect=TimeoutError("private detail")):
            self.assertFalse(METRIC.docker_voice_ready("client-dev"))

    def test_stale_or_unverifiable_native_health_never_reports_ready(self):
        fixed = datetime(2026, 10, 27, tzinfo=timezone.utc)

        def observed(end, *, include_log=True):
            health = {"Status": "healthy"}
            if include_log:
                health["Log"] = [{"End": end, "ExitCode": 0}]
            with mock.patch.object(METRIC.subprocess, "run", side_effect=[
                result("a1\n"), result(json.dumps({"Running": True, "Health": health}))
            ]):
                return METRIC.docker_voice_ready("client-dev", now=fixed)

        # Docker can retain a 'healthy' state after its probe loop stops.
        # Neither stale nor untrusted probe timestamps may publish a healthy metric.
        for stamp in ("2026-10-26T23:57:59Z", "2026-10-27T00:00:01Z",
                      "not-a-time", "", None, "2026-10-27T00:00:00"):
            with self.subTest(stamp=stamp):
                self.assertFalse(observed(stamp))
        self.assertFalse(observed("2026-10-27T00:00:00Z", include_log=False))
        self.assertTrue(observed("2026-10-26T23:58:00Z"))  # exactly 120 seconds
        self.assertTrue(observed("2026-10-26T23:59:59Z"))

    def test_recent_failed_or_unverifiable_native_probe_is_not_healthy(self):
        # Docker keeps Status=healthy until its configured failure threshold is
        # reached. A failed latest probe must not be reported as voice-ready.
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        stamp = now.isoformat()

        def observed(log):
            state = {"Running": True, "Health": {"Status": "healthy", "Log": log}}
            with mock.patch.object(METRIC.subprocess, "run", side_effect=[
                result("a1\n"), result(json.dumps(state))
            ]):
                return METRIC.docker_voice_ready("client-dev", now=now)

        previous_ok = {"End": stamp, "ExitCode": 0}
        self.assertTrue(observed([previous_ok]))
        for code in (1, 2, 127, None, "0", False):
            with self.subTest(exit_code=code):
                self.assertFalse(observed([previous_ok, {"End": stamp, "ExitCode": code}]))
        self.assertFalse(observed([previous_ok, {"End": stamp}]))
        self.assertFalse(observed([previous_ok, {"End": stamp, "ExitCode": {"value": 0}}]))

    def test_payload_is_bounded_boolean_and_contains_no_call_or_container_details(self):
        at = datetime(2026, 10, 26, tzinfo=timezone.utc)
        data = METRIC.build_timeseries("sample-project-123", "123456", "asia-southeast1-a", "acme", "dev", False, at)
        series = data["timeSeries"][0]
        self.assertEqual(series["metric"]["type"], "custom.googleapis.com/model_garden/voice_ready")
        self.assertEqual(series["resource"]["type"], "gce_instance")
        self.assertEqual(series["points"][0]["value"]["doubleValue"], 0.0)
        self.assertEqual(series["metric"]["labels"], {"client": "acme", "environment": "dev"})
        self.assertNotIn("call", json.dumps(data).lower())
        self.assertEqual(METRIC.build_timeseries("sample-project-123", "123456", "asia-southeast1-a", "acme", "dev", True, at)["timeSeries"][0]["points"][0]["value"]["doubleValue"], 1.0)

    def test_metadata_and_metric_endpoint_are_fixed_and_token_never_enters_payload(self):
        calls = []
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit=-1): return self.value
        def open_request(req, timeout):
            calls.append(req)
            if "service-accounts/default/token" in req.full_url:
                return Response(b'{"access_token":"synthetic-secret"}')
            if "projects/sample-project-123/timeSeries" in req.full_url:
                self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-secret")
                self.assertNotIn(b"synthetic-secret", req.data)
                return Response(b"{}")
            raise AssertionError(req.full_url)
        with mock.patch.object(METRIC, "_open", side_effect=open_request):
            METRIC.publish_sample("sample-project-123", {"timeSeries": []})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.full_url.startswith(("http://metadata.google.internal/computeMetadata/v1/", "https://monitoring.googleapis.com/v3/")) for c in calls))

    def test_metadata_and_monitoring_requests_disable_proxy_and_redirects(self):
        req = METRIC.Request("https://monitoring.googleapis.com/v3/projects/sample-project-123/timeSeries")
        with mock.patch.object(METRIC, "build_opener") as factory:
            METRIC._open(req, 3)
        handlers = factory.call_args.args
        self.assertTrue(any(isinstance(h, METRIC.ProxyHandler) and h.proxies == {} for h in handlers))
        redirect = next(h for h in handlers if isinstance(h, METRIC._NoRedirect))
        with self.assertRaises(ValueError):
            redirect.redirect_request(None, req, 302, "", {}, "https://attacker.invalid",)
        factory.return_value.open.assert_called_once_with(req, timeout=3)

    def test_invalid_identifiers_are_rejected_before_any_network_or_docker(self):
        for project, client, env, compose in [
            ("other/project", "acme", "dev", "acme-dev"),
            ("sample-project-123", "acme/secret", "dev", "acme-dev"),
            ("sample-project-123", "acme", "prod;evil", "acme-dev"),
            ("sample-project-123", "acme", "dev", "bad project"),
        ]:
            with self.subTest(project=project, client=client, env=env, compose=compose):
                with self.assertRaises(ValueError):
                    METRIC.validate_inputs(project, client, env, compose)


if __name__ == "__main__":
    unittest.main()
