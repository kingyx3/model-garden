from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "platform" / "metering" / "lago.py"
spec = importlib.util.spec_from_file_location("lago_metering", MODULE)
lago = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(lago)


class FakeTransport:
    def __init__(self, status=200, response=None):
        self.status = status
        self.response = response if response is not None else {"event": {"lago_id": "evt-1"}}
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        return self.status, self.response


class LagoMeteringTests(unittest.TestCase):
    def test_build_usage_event_preserves_idempotency_and_usage_dimensions(self):
        payload = lago.build_usage_event(
            transaction_id="inf_20260915_acme_0001",
            external_subscription_id="acme-prod",
            code="llm_usage",
            timestamp=1789466400,
            properties={
                "client": "acme",
                "agent": "sales-assistant",
                "environment": "prod",
                "provider": "openai",
                "model": "approved-openai-model",
                "tokens_in": 1200,
                "tokens_out": 300,
                "provider_cost_cents": 2.75,
            },
        )
        event = payload["event"]
        self.assertEqual(event["transaction_id"], "inf_20260915_acme_0001")
        self.assertEqual(event["external_subscription_id"], "acme-prod")
        self.assertEqual(event["code"], "llm_usage")
        self.assertEqual(event["properties"]["agent"], "sales-assistant")
        self.assertEqual(event["properties"]["provider_cost_cents"], 2.75)
        self.assertNotIn("prompt", event["properties"])
        self.assertNotIn("response", event["properties"])

    def test_client_posts_only_usage_facts_to_lago_event_endpoint(self):
        transport = FakeTransport()
        client = lago.LagoClient("runtime-lago-secret", base_url="https://lago.example", transport=transport)
        response = client.send_usage(
            transaction_id="voice_acme_0001",
            external_subscription_id="acme-prod",
            code="voice_minutes",
            timestamp=1789466400,
            properties={"client": "acme", "agent": "receptionist", "minutes": 4.5},
        )
        self.assertEqual(response["event"]["lago_id"], "evt-1")
        method, url, headers, payload = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://lago.example/api/v1/events")
        self.assertEqual(headers["Authorization"], "Bearer runtime-lago-secret")
        self.assertNotIn("runtime-lago-secret", repr(payload))
        self.assertEqual(payload["event"]["properties"]["minutes"], 4.5)

    def test_adapter_does_not_compute_markup_or_invoice_amounts(self):
        payload = lago.build_usage_event(
            transaction_id="cost_acme_1",
            external_subscription_id="acme-prod",
            code="provider_cost",
            timestamp=1789466400,
            properties={"provider_cost_cents": 10.0},
        )
        self.assertNotIn("markup", payload["event"])
        self.assertNotIn("price", payload["event"])
        self.assertNotIn("invoice", payload["event"])

    def test_invalid_inputs_and_failed_ingestion_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "transaction_id"):
            lago.build_usage_event(transaction_id="", external_subscription_id="acme", code="usage")
        with self.assertRaisesRegex(ValueError, "JSON-serializable"):
            lago.build_usage_event(
                transaction_id="txn",
                external_subscription_id="acme",
                code="usage",
                properties={"bad": object()},
            )
        client = lago.LagoClient("secret", transport=FakeTransport(status=422, response={"error": "invalid"}))
        with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
            client.send_usage(transaction_id="txn", external_subscription_id="acme", code="usage")


if __name__ == "__main__":
    unittest.main()
