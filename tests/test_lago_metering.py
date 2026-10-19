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
    def test_build_attribution_properties_supports_showback_and_shared_pool(self):
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
            currency="sgd",
            trace_id="call-123",
            action_request_id="a" * 64,
            cost_center="front-office",
            billing_subscription_id="acme-enterprise-2026",
            shared_pool_id="acme-ai-workforce",
            extra={"tokens_in": 1200, "tokens_out": 300},
        )
        self.assertEqual(properties["client"], "acme")
        self.assertEqual(properties["agent"], "receptionist")
        self.assertEqual(properties["currency"], "SGD")
        self.assertEqual(properties["action_request_id"], "a" * 64)
        self.assertEqual(properties["shared_pool_id"], "acme-ai-workforce")
        self.assertEqual(properties["provider_cost_cents"], 2.75)
        self.assertNotIn("markup", properties)
        self.assertNotIn("price", properties)

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

    def test_sensitive_content_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "sensitive field"):
            lago.build_attribution_properties(
                client="acme",
                agent="receptionist",
                environment="prod",
                provider="openai",
                extra={"prompt": "do not meter this"},
            )
        with self.assertRaisesRegex(ValueError, "sensitive field"):
            lago.build_usage_event(
                transaction_id="voice_acme_0002",
                external_subscription_id="acme-prod",
                code="voice_minutes",
                properties={"transcript": "customer content"},
            )

    def test_client_posts_usage_facts_in_batches_to_the_lago_batch_endpoint(self):
        transport = FakeTransport(response={"events": []})
        client = lago.LagoClient("runtime-lago-secret", base_url="https://lago.example", transport=transport)
        events = [lago.build_usage_event(transaction_id=f"txn-{n}", external_subscription_id="acme-prod", code="tool_calls",
                                         timestamp=1789466400, properties={"quantity": 1})["event"] for n in range(150)]
        self.assertEqual(client.send_events(events), {"accepted": 150, "alreadyPresent": 0})
        self.assertEqual([len(call[3]["events"]) for call in transport.calls], [100, 50])
        method, url, headers, payload = transport.calls[0]
        self.assertEqual((method, url), ("POST", "https://lago.example/api/v1/events/batch"))
        self.assertEqual(headers["Authorization"], "Bearer runtime-lago-secret")
        self.assertNotIn("runtime-lago-secret", repr(payload))

    def test_events_lago_already_holds_are_dropped_and_the_rest_resent(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append([event["transaction_id"] for event in payload["events"]])
            if len(calls) == 1:
                return 422, {"code": "validation_errors", "error_details": {"1": {"transaction_id": ["value_already_exist"]}}}
            return 200, {"events": []}

        client = lago.LagoClient("k", base_url="http://127.0.0.1:3000", transport=transport)
        events = [{"transaction_id": name} for name in ("a", "b", "c")]
        self.assertEqual(client.send_events(events), {"accepted": 2, "alreadyPresent": 1})
        self.assertEqual(calls, [["a", "b", "c"], ["a", "c"]])

    def test_any_other_rejection_fails_without_dropping_events(self):
        client = lago.LagoClient("k", base_url="http://127.0.0.1:3000", transport=FakeTransport(
            status=422, response={"error_details": {"0": {"transaction_id": ["value_already_exist"]}, "1": {"code": ["value_is_mandatory"]}}}))
        with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
            client.send_events([{"transaction_id": "a"}, {"transaction_id": "b"}])

    def test_billable_metrics_are_created_then_left_alone_then_updated(self):
        definition = {"code": "tool_calls", "name": "Governed Tool calls", "aggregation_type": "sum_agg", "field_name": "quantity",
                      "recurring": False, "filters": [{"key": "agent", "values": ["receptionist"]}]}
        stored: dict = {}

        def transport(method, url, headers, payload):
            if method == "GET":
                return (200, {"billable_metric": stored["metric"]}) if stored else (404, {})
            stored["metric"] = {**payload["billable_metric"], "lago_id": "bm-1",
                                "filters": [{"key": f["key"], "values": list(reversed(f["values"]))} for f in payload["billable_metric"]["filters"]]}
            return 200, {"billable_metric": stored["metric"]}

        client = lago.LagoClient("k", base_url="http://127.0.0.1:3000", transport=transport)
        self.assertEqual(client.ensure_metric(definition), "created")
        self.assertEqual(client.ensure_metric(definition), "unchanged")
        widened = {**definition, "filters": [{"key": "agent", "values": ["receptionist", "sales"]}]}
        self.assertEqual(client.ensure_metric(widened), "updated")
        broken = lago.LagoClient("k", base_url="http://127.0.0.1:3000", transport=FakeTransport(status=401, response={}))
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            broken.ensure_metric(definition)

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
        client = lago.LagoClient("secret", base_url="https://lago.example", transport=FakeTransport(status=422, response={"error": "invalid"}))
        with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
            client.send_events([{"transaction_id": "txn"}])
        with self.assertRaisesRegex(ValueError, "embedded credentials"):
            lago.LagoClient("secret", base_url="https://user:pass@lago.example")


if __name__ == "__main__":
    unittest.main()
