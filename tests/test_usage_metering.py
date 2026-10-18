"""Credential-free checks for metering model usage from the Hermes session stores into the ledger."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("usage_metering_under_test", ROOT / "platform/metering/usage.py")
usage = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = usage
spec.loader.exec_module(usage)

# The columns of the pinned Hermes sessions table that metering reads, plus ones it must never copy.
SCHEMA = """CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, chat_id TEXT, title TEXT,
system_prompt TEXT, model TEXT, billing_provider TEXT, started_at REAL NOT NULL, last_activity_at REAL,
input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0, api_call_count INTEGER DEFAULT 0,
estimated_cost_usd REAL, actual_cost_usd REAL)"""


def store(path: pathlib.Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=wal")
    connection.execute(SCHEMA)
    connection.commit()
    return connection


def upsert(connection, session, *, source="slack", tokens=(0, 0), calls=0, cost=None, actual=None, model="gpt-5.1", at=1_800_000_000):
    connection.execute(
        "INSERT INTO sessions (id, source, user_id, chat_id, title, system_prompt, model, billing_provider, started_at, last_activity_at, "
        "input_tokens, output_tokens, api_call_count, estimated_cost_usd, actual_cost_usd) "
        "VALUES (?, ?, 'U-PRIVATE', 'C-PRIVATE', 'Card 4111 1111 1111 1111', 'secret prompt', ?, 'openai', 1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, "
        "api_call_count = excluded.api_call_count, estimated_cost_usd = excluded.estimated_cost_usd, "
        "actual_cost_usd = excluded.actual_cost_usd, last_activity_at = excluded.last_activity_at",
        (session, source, model, at, tokens[0], tokens[1], calls, cost, actual),
    )
    connection.commit()


class UsageMeterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.db = self.root / "state.db"
        self.hermes = store(self.db)
        self.addCleanup(self.hermes.close)
        self.meter = usage.UsageMeter(self.root / "worker" / "cursor.sqlite")

    def collect(self):
        facts = self.meter.collect("conversations", usage.read_sessions(self.db))
        self.meter.commit(facts)
        return [fact for fact in facts if "usageId" in fact]

    def test_each_pass_meters_only_what_each_session_gained(self):
        upsert(self.hermes, "s1", tokens=(1000, 200), calls=2, cost=0.0125)
        first = self.collect()
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["inputTokens"], first[0]["outputTokens"], first[0]["apiCalls"]), (1000, 200, 2))
        self.assertEqual((first[0]["costMicrosUsd"], first[0]["costSource"]), (12_500, "estimated"))
        self.assertEqual(self.collect(), [])  # nothing changed, nothing metered
        upsert(self.hermes, "s1", tokens=(1500, 260), calls=3, cost=0.02)
        upsert(self.hermes, "s2", source="api_server", tokens=(10, 5), calls=1)
        second = {fact["sessionId"]: fact for fact in self.collect()}
        self.assertEqual((second["s1"]["inputTokens"], second["s1"]["outputTokens"], second["s1"]["costMicrosUsd"]), (500, 60, 7_500))
        self.assertEqual((second["s2"]["channel"], second["s2"]["inputTokens"]), ("api_server", 10))

    def test_a_retry_before_commit_gives_the_same_usage_id(self):
        upsert(self.hermes, "s1", tokens=(100, 10), calls=1)
        before = self.meter.collect("conversations", usage.read_sessions(self.db))
        again = self.meter.collect("conversations", usage.read_sessions(self.db))
        self.assertEqual(before[0]["usageId"], again[0]["usageId"])
        self.meter.commit(again)
        upsert(self.hermes, "s1", tokens=(200, 20), calls=2)
        self.assertNotEqual(self.collect()[0]["usageId"], before[0]["usageId"])

    def test_a_counter_that_goes_down_starts_a_new_baseline_instead_of_a_recount(self):
        upsert(self.hermes, "s1", tokens=(500, 50), calls=1)
        self.collect()
        upsert(self.hermes, "s1", tokens=(100, 10), calls=1)
        self.assertEqual(self.collect(), [])
        upsert(self.hermes, "s1", tokens=(150, 10), calls=2)
        self.assertEqual([(fact["inputTokens"], fact["apiCalls"]) for fact in self.collect()], [(50, 1)])

    def test_actual_cost_wins_over_the_estimate(self):
        upsert(self.hermes, "s1", tokens=(1, 1), calls=1, cost=0.5, actual=0.25)
        fact = self.collect()[0]
        self.assertEqual((fact["costMicrosUsd"], fact["costSource"]), (250_000, "actual"))

    def test_ledger_details_carry_counts_and_names_only(self):
        upsert(self.hermes, "session-with-a-name", tokens=(1200, 300), calls=2, cost=0.0123)
        details = usage.ledger_details(self.collect()[0])
        text = json.dumps(details)
        for private in ("U-PRIVATE", "C-PRIVATE", "4111 1111", "secret prompt", "session-with-a-name"):
            self.assertNotIn(private, text)
        self.assertEqual(details["usage"], {"tokens": 1500, "costCents": 1})
        self.assertEqual((details["model"], details["provider"], details["channel"]), ("gpt-5.1", "openai", "slack"))
        self.assertEqual(len(details["session"]), 16)

    def test_a_store_without_the_hermes_counters_is_refused(self):
        other = self.root / "other.db"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE sessions (id TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(usage.UsageError, "token counters"):
            usage.read_sessions(other)
        with self.assertRaises(usage.UsageError):
            usage.read_sessions(self.root / "missing" / "state.db")

    def test_the_store_is_never_written(self):
        upsert(self.hermes, "s1", tokens=(1, 1))
        before = self.db.read_bytes()
        self.collect()
        self.assertEqual(self.db.read_bytes(), before)

    def test_sources_parse_from_the_environment_format(self):
        self.assertEqual(usage.parse_sources("conversations=/opt/hermes/conversations/state.db, coworker=/opt/hermes/coworker/state.db"),
                         {"conversations": pathlib.Path("/opt/hermes/conversations/state.db"),
                          "coworker": pathlib.Path("/opt/hermes/coworker/state.db")})
        self.assertEqual(usage.parse_sources(""), {})
        for bad in ("conversations", "Bad=/x", "a=relative/path", "a=/x,a=/y"):
            with self.assertRaises(ValueError):
                usage.parse_sources(bad)


if __name__ == "__main__":
    unittest.main()
