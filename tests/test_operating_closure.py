from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


scorecard = load_script("operating_scorecard", "operating-scorecard.py")
ops = load_script("operations_readiness", "operations-readiness.py")
voice_deploy = load_script("deploy_reference_voice", "deploy-reference-voice.py")
reference_proof = load_script("reference_proof", "reference-proof.py")


class OperatingClosureTests(unittest.TestCase):
    def test_scorecard_calculates_margin_ttv_and_concentration(self):
        doc = {
            "signed_delivery_backlog": 1,
            "available_delivery_capacity_hours": 40,
            "clients": [
                {
                    "client": "acme",
                    "signed_date": "2026-09-01",
                    "working_dev_date": "2026-09-03",
                    "production_date": "2026-09-10",
                    "revenue": 10000,
                    "cash_collected": 8000,
                    "loaded_hourly_cost": 100,
                    "effort_hours": {"sales": 2, "discovery": 3, "implementation": 20, "tuning": 5, "support": 0},
                    "provider_costs": {"model": 100, "voice": 50, "hosting": 50, "connectors": 0, "other": 0},
                    "work_hours": {"reusable": 10, "configurable": 15, "bespoke": 5},
                    "baseline_metric": "20 missed calls/week",
                    "post_launch_metric": "5 missed calls/week",
                    "next_expansion": "follow-up workflow",
                }
            ],
        }
        result = scorecard.summarize(doc)
        row = result["clients"][0]
        self.assertEqual(row["timeToWorkingDevDays"], 2)
        self.assertEqual(row["timeToProductionDays"], 9)
        self.assertAlmostEqual(row["directCost"], 3200)
        self.assertAlmostEqual(row["grossMargin"], 0.68)
        self.assertAlmostEqual(row["reusableOrConfigurableRatio"], 25 / 30)
        self.assertAlmostEqual(result["business"]["largestClientRevenueShare"], 1.0)
        self.assertEqual(result["business"]["receivables"], 2000)

    def test_operations_record_fails_closed_until_owners_and_policies_are_filled(self):
        template = yaml.safe_load((ROOT / "templates" / "operations" / "client-operations.yaml").read_text(encoding="utf-8"))
        result = ops.validate(template)
        self.assertFalse(result["ready"])
        self.assertIn("ownership.runtime_operator", result["missing"])
        self.assertIn("continuity.rollback_test_record", result["missing"])

        for path in result["required"]:
            current = template
            parts = path.split(".")
            for part in parts[:-1]:
                current = current[part]
            current[parts[-1]] = "recorded"
        self.assertTrue(ops.validate(template)["ready"])

    def test_voice_binding_is_optional_and_secret_scoped(self):
        self.assertIsNone(voice_deploy._voice({"spec": {}}))
        binding = {
            "spec": {
                "channels": {
                    "voice": {
                        "provider": "livekit",
                        "url": "wss://voice.example.test",
                        "api_key_ref": "secret://voice/key",
                        "api_secret_ref": "secret://voice/secret",
                        "human_transfer_target": "tel:+15550000",
                    }
                }
            }
        }
        voice = voice_deploy._voice(binding)
        self.assertEqual(voice["provider"], "livekit")
        self.assertEqual(voice["human_transfer_target"], "tel:+15550000")

    def test_voice_build_context_contains_only_runtime_dependencies_and_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = voice_deploy._context(pathlib.Path(temporary))
            self.assertTrue((context / "requirements-voice.txt").is_file())
            self.assertTrue((context / "livekit_receptionist.py").is_file())
            dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("ARG BASE_IMAGE", dockerfile)
            self.assertIn("requirements-voice.txt", dockerfile)

    def test_reference_proof_requires_real_evidence_only_when_requested(self):
        evidence = {
            "services": {
                "hermes": {"running": True},
                "governed-tools": {"running": True},
                "voice": {"running": True},
            },
            "governance": {"records": 2},
            "voice": {"distinctCalls": 1, "humanTransferEvidence": 0, "messageFallbackEvidence": 1},
        }
        result = reference_proof.evaluate(
            evidence,
            require_voice_call=True,
            require_fallback=True,
            require_governed_action=True,
        )
        self.assertTrue(result["passed"])

        evidence["voice"]["distinctCalls"] = 0
        result = reference_proof.evaluate(
            evidence,
            require_voice_call=True,
            require_fallback=True,
            require_governed_action=True,
        )
        self.assertFalse(result["passed"])
        self.assertIn("no real voice call lifecycle evidence exists", result["failures"])

    def test_reference_proof_summaries_do_not_copy_sensitive_payloads(self):
        voice = reference_proof._voice_summary([
            {"type": "CallStarted", "room": "room-1", "caller_number": "+15550001"},
            {"type": "FallbackMessage", "room": "room-1", "message": "private message"},
        ])
        rendered = repr(voice)
        self.assertEqual(voice["distinctCalls"], 1)
        self.assertEqual(voice["messageFallbackEvidence"], 1)
        self.assertNotIn("+15550001", rendered)
        self.assertNotIn("private message", rendered)


if __name__ == "__main__":
    unittest.main()
