from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "platform" / "metering" / "lago.py"
SCHEMA = ROOT / "contracts" / "v1" / "usage-attribution.schema.json"

spec = importlib.util.spec_from_file_location("lago_metering_contract", MODULE)
lago = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(lago)


class UsageAttributionContractTests(unittest.TestCase):
    def test_runtime_builder_conforms_to_versioned_contract_with_metering_extensions(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        properties = lago.build_attribution_properties(
            client="acme",
            agent="receptionist",
            environment="prod",
            provider="openai",
            model="approved-openai-model",
            usage_type="llm_tokens",
            quantity=1500,
            unit="tokens",
            provider_cost_cents=2.75,
            currency="SGD",
            trace_id="call-123",
            action_request_id="a" * 64,
            cost_center="front-office",
            billing_subscription_id="acme-enterprise-2026",
            shared_pool_id="acme-ai-workforce",
            extra={"tokens_in": 1200, "tokens_out": 300},
        )
        Draft202012Validator(schema).validate(properties)

    def test_contract_rejects_missing_core_attribution_dimensions(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors({"client": "acme", "agent": "receptionist"}))
        self.assertTrue(errors)
        self.assertTrue(any("environment" in error.message for error in errors))
        self.assertTrue(any("provider" in error.message for error in errors))


if __name__ == "__main__":
    unittest.main()
