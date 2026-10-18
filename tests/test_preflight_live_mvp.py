from __future__ import annotations

import importlib.util
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "preflight-live-mvp.py"
spec = importlib.util.spec_from_file_location("preflight_live_mvp", MODULE_PATH)
preflight = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(preflight)


class LiveMvpPreflightTests(unittest.TestCase):
    def complete_env(self):
        return {
            "OPENAI_API_KEY": "model-secret",
            "LIVEKIT_URL": "wss://livekit.example.test",
            "LIVEKIT_API_KEY": "livekit-key",
            "LIVEKIT_API_SECRET": "livekit-secret",
            "LIVEKIT_SIP_TRUNK_ID": "trunk-id",
        }

    @mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/docker")
    def test_calendar_never_passes_without_upstream_proof(self, _which):
        result = preflight.check(self.complete_env())
        self.assertFalse(result["ready"])
        self.assertIn("approved upstream MCP execution", result["missing"]["calendar"][0])

    @mock.patch.object(preflight.shutil, "which", return_value=None)
    def test_reports_missing_capabilities_without_secret_values(self, _which):
        env = self.complete_env()
        del env["LIVEKIT_API_SECRET"]
        result = preflight.check(env)
        self.assertFalse(result["ready"])
        self.assertIn("approved upstream MCP execution", result["missing"]["calendar"][0])
        self.assertEqual(result["missing"]["voice"], ["LIVEKIT_API_SECRET"])
        self.assertEqual(result["missing"]["runtime"], ["docker"])
        rendered = repr(result)
        self.assertNotIn("model-secret", rendered)
        self.assertNotIn("livekit-key", rendered)

    def test_whitespace_binding_counts_as_missing(self):
        env = self.complete_env()
        env["OPENAI_API_KEY"] = "   "
        result = preflight.check(env, require_docker=False)
        self.assertEqual(result["missing"]["model"], ["OPENAI_API_KEY"])


if __name__ == "__main__":
    unittest.main()
