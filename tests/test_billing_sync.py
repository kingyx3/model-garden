"""Credential-free checks for the billing sync: exported ledgers to Lago usage events and a per-Agent usage report."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


billing = _load("billing_sync_under_test", "platform/metering/billing.py")
govern = _load("billing_sync_govern", "scripts/govern-action.py")
export = billing.ledger()
OCTOBER, NOVEMBER = 1_792_000_000, 1_794_700_000  # 2026-10-14 and 2026-11-14, UTC
REQUEST = "a" * 64


def usage_fact(usage_id: str, *, agent="receptionist", channel="slack", tokens=(1000, 200), at=OCTOBER):
    return {"usageId": usage_id, "agent": agent, "surface": "conversations", "session": "0" * 16, "channel": channel,
            "provider": "openai", "model": "gpt-5.1", "occurredAt": at, "inputTokens": tokens[0], "outputTokens": tokens[1],
            "cacheReadTokens": 0, "cacheWriteTokens": 0, "reasoningTokens": 0, "apiCalls": 1, "costMicrosUsd": 1500,
            "usage": {"tokens": sum(tokens), "costCents": 0}}


def execution(tool="crm.create", outcome="succeeded"):
    return {"schemaVersion": 3, "timestamp": "2026-10-14T10:00:00Z", "event": "execution", "outcome": outcome,
            "requestId": REQUEST, "agent": "receptionist", "tool": tool, "arguments": {}, "argumentsDigest": "d",
            "governance": {"channel": "web"}, "ledger": {"provider": "built-in-crm"}}


class FakeLago:
    def __init__(self):
        self.events: list[dict] = []

    def send_events(self, events):
        known = {event["transaction_id"] for event in self.events}
        fresh = [event for event in events if event["transaction_id"] not in known]
        self.events.extend(fresh)
        return {"accepted": len(fresh), "alreadyPresent": len(events) - len(fresh)}


class BillingSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.state = self.root / "state"
        self.ledgers: dict[str, pathlib.Path] = {}
        self.config = billing.load_config({
            "lago": {"url": "http://127.0.0.1:3000"}, "stateDir": str(self.state),
            "sources": [
                {"client": "acme", "environment": "prod", "subscription": "acme-2026", "bucket": "acme-prod-ledger", "agents": ["receptionist"]},
                {"client": "globex", "environment": "prod", "subscription": "globex-2026", "bucket": "globex-prod-ledger", "agents": ["sales"]},
            ],
        })
        self.lago = FakeLago()

    def ledger(self, bucket: str) -> pathlib.Path:
        return self.ledgers.setdefault(bucket, self.root / bucket / "audit.jsonl")

    def record_usage(self, bucket: str, *facts):
        for fact in facts:
            govern.record_operator_action(self.ledger(bucket), "usage.recorded", "runtime:worker", fact)

    def export(self, bucket: str):
        export.export_ledger(self.ledger(bucket), export.DirectorySink(self.root / "sinks" / bucket), self.root / bucket / "export-state.json")

    def sink(self, source):
        return export.DirectorySink(self.root / "sinks" / source["bucket"])

    def sync(self):
        return billing.sync(self.config, sinks=self.sink, client=self.lago)

    def test_usage_and_tool_calls_become_lago_events_once_per_agent_and_token_kind(self):
        self.record_usage("acme-prod-ledger", usage_fact("u1"))
        govern.append_audit_event(self.ledger("acme-prod-ledger"), execution())
        govern.append_audit_event(self.ledger("acme-prod-ledger"), execution(outcome="failed"))
        self.record_usage("globex-prod-ledger", usage_fact("u2", agent="sales", tokens=(10, 0)))
        for bucket in self.ledgers:
            self.export(bucket)
        report = self.sync()
        self.assertEqual(report["acme-prod"], {"records": 3, "rows": 2, "accepted": 3, "alreadyPresent": 0})
        by_code = {(event["external_subscription_id"], event["code"]): event for event in self.lago.events}
        self.assertEqual(sorted(by_code), [("acme-2026", "llm_input_tokens"), ("acme-2026", "llm_output_tokens"),
                                           ("acme-2026", "tool_calls"), ("globex-2026", "llm_input_tokens")])
        tokens = by_code[("acme-2026", "llm_input_tokens")]
        self.assertEqual(tokens["transaction_id"], "u1:input")
        self.assertEqual(tokens["timestamp"], OCTOBER)
        self.assertEqual({key: tokens["properties"][key] for key in ("client", "agent", "environment", "channel", "quantity", "model")},
                         {"client": "acme", "agent": "receptionist", "environment": "prod", "channel": "slack", "quantity": 1000, "model": "gpt-5.1"})
        tool = by_code[("acme-2026", "tool_calls")]
        self.assertEqual((tool["properties"]["tool"], tool["properties"]["provider"], tool["properties"]["quantity"]), ("crm.create", "built-in-crm", 1))
        self.assertEqual(self.sync()["acme-prod"], {"records": 0, "rows": 0, "events": 0})  # cursor advanced, nothing resent
        self.record_usage("acme-prod-ledger", usage_fact("u3", tokens=(5, 5), at=NOVEMBER))
        self.export("acme-prod-ledger")
        self.assertEqual(self.sync()["acme-prod"]["accepted"], 2)

    def test_the_report_totals_each_agent_per_month_and_counts_a_retried_row_once(self):
        self.record_usage("acme-prod-ledger", usage_fact("u1"), usage_fact("u2", channel="web", tokens=(1, 1)),
                          usage_fact("u3", tokens=(7, 3), at=NOVEMBER))
        govern.append_audit_event(self.ledger("acme-prod-ledger"), execution())
        self.export("acme-prod-ledger")
        self.sync()
        billing._journal(self.state, billing.billable_rows(self.sink(self.config["sources"][0]).records(), self.config["sources"][0]))
        rows = billing.usage_report(self.state, since="2026-10", until="2026-11")
        self.assertEqual([(row["month"], row["agent"], row["inputTokens"], row["outputTokens"], row["toolCalls"]) for row in rows],
                         [("2026-10", "receptionist", 1001, 201, 1), ("2026-11", "receptionist", 7, 3, 0)])
        by_channel = billing.usage_report(self.state, since="2026-10", until="2026-10", by=("channel",))
        self.assertEqual({row["channel"]: row["inputTokens"] for row in by_channel}, {"slack": 1000, "web": 1})
        with self.assertRaises(billing.BillingError):
            billing.usage_report(self.state, since="2026-13", until="2026-13")

    def test_an_altered_export_fails_its_source_only_and_bills_nothing_from_it(self):
        self.record_usage("acme-prod-ledger", usage_fact("u1"))
        self.record_usage("globex-prod-ledger", usage_fact("u2", agent="sales"))
        for bucket in self.ledgers:
            self.export(bucket)
        stored = next((self.root / "sinks" / "acme-prod-ledger" / "records").glob("*.json"))
        record = json.loads(stored.read_text())
        record["event"]["details"]["inputTokens"] = 999_999
        stored.write_text(json.dumps(record))
        with self.assertRaisesRegex(billing.BillingError, "acme-prod.*does not match its chain hash"):
            self.sync()
        self.assertEqual({event["external_subscription_id"] for event in self.lago.events}, {"globex-2026"})
        self.assertFalse((self.state / "cursors" / "acme-prod.json").exists())

    def test_a_gap_in_the_export_stops_billing_at_the_gap(self):
        self.record_usage("acme-prod-ledger", usage_fact("u1"), usage_fact("u2"), usage_fact("u3"))
        self.export("acme-prod-ledger")
        sorted((self.root / "sinks" / "acme-prod-ledger" / "records").glob("*.json"))[1].unlink()
        report = self.sync()
        self.assertEqual(report["acme-prod"]["records"], 1)
        self.assertEqual(json.loads((self.state / "cursors" / "acme-prod.json").read_text())["sequence"], 1)

    def test_a_dry_run_sends_and_records_nothing(self):
        self.record_usage("acme-prod-ledger", usage_fact("u1"))
        self.export("acme-prod-ledger")
        report = billing.sync(self.config, sinks=self.sink, client=None, dry_run=True)
        self.assertEqual(report["acme-prod"], {"records": 1, "rows": 1, "events": 2, "dryRun": True})
        self.assertEqual(self.lago.events, [])
        self.assertFalse((self.state / "cursors").exists())

    def test_metrics_carry_an_agent_filter_and_the_config_holds_no_secret(self):
        definitions = {metric["code"]: metric for metric in billing.metric_definitions(["sales", "receptionist", "sales"])}
        self.assertEqual(sorted(definitions), ["llm_cache_read_tokens", "llm_cache_write_tokens", "llm_input_tokens", "llm_output_tokens", "tool_calls"])
        self.assertEqual(definitions["tool_calls"]["filters"], [{"key": "agent", "values": ["receptionist", "sales"]}])
        self.assertEqual(definitions["llm_input_tokens"]["aggregation_type"], "sum_agg")
        base = {"lago": {"url": "http://127.0.0.1:3000"}, "stateDir": "/var/lib/billing",
                "sources": [{"client": "acme", "environment": "prod", "subscription": "acme-2026", "bucket": "acme-ledger"}]}
        billing.load_config(base)
        for bad in ({**base, "lago": {"url": "http://127.0.0.1:3000", "apiKey": "x"}},
                    {**base, "stateDir": "relative"},
                    {**base, "sources": base["sources"] * 2},
                    {**base, "sources": [{**base["sources"][0], "bucket": "UPPER"}]},
                    {**base, "sources": [{**base["sources"][0], "agents": ["Not A Slug"]}]}):
            with self.assertRaises((billing.BillingError, ValueError)):
                billing.load_config(bad)

    def test_the_cli_reports_csv_from_the_journal(self):
        self.record_usage("acme-prod-ledger", usage_fact("u1"))
        self.export("acme-prod-ledger")
        self.sync()
        config = self.root / "billing.yaml"
        config.write_text(yaml.safe_dump({"lago": {"url": "http://127.0.0.1:3000"}, "stateDir": str(self.state),
                                          "sources": [{"client": "acme", "environment": "prod", "subscription": "acme-2026", "bucket": "acme-prod-ledger"}]}))
        result = subprocess.run([sys.executable, str(ROOT / "scripts/billing-sync.py"), "report", "--config", str(config), "--since", "2026-10",
                                 "--format", "csv"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("month,client,environment,agent,inputTokens"))
        self.assertIn("2026-10,acme,prod,receptionist,1000,200", result.stdout)
        missing_key = subprocess.run([sys.executable, str(ROOT / "scripts/billing-sync.py"), "metrics", "--config", str(config)],
                                     capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(missing_key.returncode, 1)
        self.assertIn("LAGO_API_KEY is not set", missing_key.stderr)


if __name__ == "__main__":
    unittest.main()
