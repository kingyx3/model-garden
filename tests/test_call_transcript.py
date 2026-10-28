from __future__ import annotations

import ast
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("call_transcript_under_test", ROOT / "platform" / "channels" / "call_transcript.py")
transcript_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = transcript_module  # dataclasses resolve string annotations through sys.modules
spec.loader.exec_module(transcript_module)
T = transcript_module

VISA = "4111 1111 1111 1111"  # Luhn-valid test number
NOT_A_CARD = "4111 1111 1111 1112"  # fails Luhn


def config(tmp: str, **overrides) -> T.TranscriptConfig:
    values = {"path": pathlib.Path(tmp) / "data" / "transcripts.jsonl"}
    values.update(overrides)
    return T.TranscriptConfig(**values)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def call(tmp: str, **overrides) -> tuple[T.CallTranscript, Clock]:
    clock = Clock()
    return T.CallTranscript("callhash", "receptionist", config(tmp, **overrides), started=1_700_000_000, clock=clock), clock


class TurnTests(unittest.TestCase):
    def test_both_directions_are_ordered_with_offsets_and_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, clock = call(tmp)
            transcript.add_turn("agent", "Hello, how can I help?")
            clock.now = 103.5
            transcript.add_turn("caller", "I need a booking", confidence=0.93)
            clock.now = 106.0
            transcript.add_turn("agent", "Certainly, I can book you in for", interrupted=True)
            record = transcript.build()
        turns = record["turns"]
        self.assertEqual([turn["speaker"] for turn in turns], ["agent", "caller", "agent"])
        self.assertEqual([turn["index"] for turn in turns], [0, 1, 2])
        self.assertEqual([turn["offsetSeconds"] for turn in turns], [0.0, 3.5, 6.0])
        self.assertEqual(turns[1]["confidence"], 0.93)
        self.assertTrue(turns[2]["interrupted"])
        self.assertNotIn("interrupted", turns[0])

    def test_out_of_order_offsets_are_sorted_stably(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "second", offset=5.0)
            transcript.add_turn("agent", "first", offset=1.0)
            transcript.add_turn("caller", "also second", offset=5.0)
            turns = transcript.build()["turns"]
        self.assertEqual([turn["text"] for turn in turns], ["first", "second", "also second"])
        self.assertEqual([turn["index"] for turn in turns], [0, 1, 2])

    def test_unknown_speaker_or_outcome_is_a_programmer_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            with self.assertRaises(ValueError):
                transcript.add_turn("system", "x")
            with self.assertRaises(ValueError):
                transcript.build(outcome="great")

    def test_empty_text_and_out_of_range_confidence_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "   ")
            transcript.add_turn("caller", "hi", confidence=4.0)
            turns = transcript.build()["turns"]
        self.assertEqual(len(turns), 1)
        self.assertNotIn("confidence", turns[0])

    def test_turn_and_count_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "x" * (T.MAX_TURN_CHARS + 50))
            for _ in range(T.MAX_TURNS + 3):
                transcript.add_turn("agent", "ok")
            record = transcript.build()
        self.assertEqual(len(record["turns"]), T.MAX_TURNS)
        self.assertTrue(record["turns"][0]["truncated"])
        self.assertEqual(len(record["turns"][0]["text"]), T.MAX_TURN_CHARS)
        self.assertEqual(record["droppedTurns"], 4)


class ClosingTests(unittest.TestCase):
    def test_summary_outcome_and_request_ids_close_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "Book me Tuesday")
            record = transcript.build(outcome="booked", summary="Booked Tuesday 10:00.", request_ids=["a" * 64, "", 5])
        self.assertEqual((record["outcome"], record["summary"], record["requestIds"]), ("booked", "Booked Tuesday 10:00.", ["a" * 64]))
        self.assertEqual(record["type"], "CallTranscript")
        self.assertEqual(transcript.reference, "transcript:callhash")

    def test_observed_outcomes_never_downgrade_and_default_is_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            self.assertEqual(transcript.build()["outcome"], "abandoned")
            transcript.add_turn("agent", "Hello")
            self.assertEqual(transcript.build()["outcome"], "abandoned")
            transcript.add_turn("caller", "Hi, what are your hours?")
            self.assertEqual(transcript.build()["outcome"], "resolved")
            transcript.note_outcome("message-taken")
            transcript.note_outcome("transferred")
            transcript.note_outcome("resolved")
            self.assertEqual(transcript.build()["outcome"], "transferred")

    def test_summary_is_redacted_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            record = transcript.build(summary=f"Caller paid with {VISA}.")
        self.assertNotIn("4111", record["summary"])
        self.assertEqual(record["redactions"]["card"], 1)


class RedactionTests(unittest.TestCase):
    def test_luhn_valid_cards_are_redacted_in_any_common_format(self):
        for number in (VISA, "4111-1111-1111-1111", "4111111111111111"):
            redacted, counts = T.redact(f"my card is {number} thanks")
            self.assertEqual(redacted, f"my card is {T.CARD_REPLACEMENT} thanks", number)
            self.assertEqual(counts["card"], 1)

    def test_non_cards_are_left_alone(self):
        for text in (NOT_A_CARD, "call +65 6123 4567", "booking 20261020", "order 12345"):
            redacted, counts = T.redact(text)
            self.assertEqual(redacted, text, text)
            self.assertEqual(counts["card"], 0)

    def test_client_patterns_apply_and_are_counted(self):
        patterns = (re.compile(r"\b[STFG]\d{7}[A-Z]\b"),)
        redacted, counts = T.redact("NRIC S1234567D and T7654321Z", patterns)
        self.assertEqual(redacted, f"NRIC {T.CUSTOM_REPLACEMENT} and {T.CUSTOM_REPLACEMENT}")
        self.assertEqual(counts["pattern"], 2)

    def test_redaction_happens_before_storage_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", f"Pay with {VISA}")
            transcript.mark_disclosure_played()
            record = T.finalize(transcript)
            stored = config(tmp).path.read_text(encoding="utf-8")
        self.assertNotIn("4111", stored)
        self.assertTrue(record["turns"][0]["redacted"])
        self.assertEqual(record["redactions"], {"card": 1, "pattern": 0})


class ConfigTests(unittest.TestCase):
    def test_defaults_are_transcribe_with_disclosure_and_seven_day_retention(self):
        cfg = T.config_from_env({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.disclosure, T.DEFAULT_DISCLOSURE)
        self.assertEqual(cfg.retention_seconds, 7 * 24 * 60 * 60)
        self.assertEqual(cfg.path, pathlib.Path(T.DEFAULT_PATH))

    def test_transcription_cannot_run_without_disclosure_unless_a_lawful_basis_owner_is_recorded(self):
        with self.assertRaisesRegex(T.TranscriptConfigError, "disclosure"):
            T.config_from_env({"MODEL_GARDEN_TRANSCRIPT_DISCLOSURE": "off"})
        with self.assertRaises(T.TranscriptConfigError):
            T.config_from_env({"MODEL_GARDEN_TRANSCRIPT_DISCLOSURE": "  "})
        cfg = T.config_from_env({
            "MODEL_GARDEN_TRANSCRIPT_DISCLOSURE": "off",
            "MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER": "dpo@client.example",
        })
        self.assertIsNone(cfg.disclosure)
        self.assertEqual(cfg.override_owner, "dpo@client.example")
        off = T.config_from_env({"MODEL_GARDEN_TRANSCRIPT": "off", "MODEL_GARDEN_TRANSCRIPT_DISCLOSURE": "off"})
        self.assertFalse(off.enabled)

    def test_override_is_recorded_on_the_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp, disclosure=None, override_owner="dpo@client.example")
            self.assertIsNone(transcript.disclosure_text)
            disclosure = transcript.build()["disclosure"]
        self.assertEqual(disclosure, {"configured": False, "played": False, "lawfulBasisOverrideOwner": "dpo@client.example"})

    def test_disclosure_state_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            self.assertEqual(transcript.disclosure_text, T.DEFAULT_DISCLOSURE)
            transcript.mark_disclosure_played()
            self.assertEqual(transcript.build()["disclosure"], {"configured": True, "played": True})

    def test_invalid_settings_fail_closed(self):
        for env in (
            {"MODEL_GARDEN_TRANSCRIPT": "maybe"},
            {"MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS": "-1"},
            {"MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS": "soon"},
            {"MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS": str(T.MAX_RETENTION_SECONDS + 1)},
            {"MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS": "not json"},
            {"MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS": '["(unclosed"]'},
            {"MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS": json.dumps(["x" * (T.MAX_PATTERN_CHARS + 1)])},
            {"MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS": json.dumps(["a"] * (T.MAX_PATTERNS + 1))},
        ):
            with self.assertRaises(T.TranscriptConfigError, msg=str(env)):
                T.config_from_env(env)

    def test_client_patterns_load_from_environment(self):
        cfg = T.config_from_env({"MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS": json.dumps([r"\bS\d{7}[A-Z]\b"])})
        self.assertEqual(len(cfg.patterns), 1)


class StorageTests(unittest.TestCase):
    def test_unplayed_disclosure_cannot_persist_call_content_or_report_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "sensitive caller speech")
            failures: list[str] = []
            self.assertIsNone(T.finalize(transcript, on_failure=failures.append, auto_summary=True))
            self.assertTrue(transcript.suppressed)
            self.assertFalse(config(tmp).path.exists())
            self.assertEqual(failures, [], "privacy suppression is not a failed disk write")

    def test_disclosure_override_allows_no_spoken_notice_only_with_recorded_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp, disclosure=None, override_owner="dpo@client.example")
            transcript.add_turn("caller", "approved lawful basis")
            record = T.finalize(transcript)
            self.assertIsNotNone(record)
            self.assertEqual(record["disclosure"]["lawfulBasisOverrideOwner"], "dpo@client.example")
            self.assertTrue(config(tmp).path.exists())
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp, disclosure=None, override_owner=None)
            transcript.add_turn("caller", "must not persist")
            self.assertIsNone(T.finalize(transcript))
            self.assertFalse(config(tmp).path.exists())

    def test_spoken_disclosure_allows_storage_and_suppression_cannot_be_reversed(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "after notice")
            transcript.mark_disclosure_played()
            self.assertIsNotNone(T.finalize(transcript))
            self.assertTrue(config(tmp).path.exists())
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.suppress()
            transcript.mark_disclosure_played()
            self.assertIsNone(T.finalize(transcript))
            self.assertFalse(config(tmp).path.exists())

    def test_record_is_stored_with_restrictive_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "hello")
            transcript.mark_disclosure_played()
            self.assertIsNotNone(T.finalize(transcript))
            path = config(tmp).path
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["callHash"], "callhash")

    def test_retention_zero_stores_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp, retention_seconds=0)
            transcript.add_turn("caller", "hello")
            transcript.mark_disclosure_played()
            T.finalize(transcript)
            self.assertFalse(config(tmp).path.exists())

    def test_corrupt_existing_voice_record_is_not_silently_deleted(self):
        # Both transcripts and fallback messages share this retention writer.
        # A damaged historical row must not disappear when the next call ends.
        for invalid in ("not json", "[]", '{"timestamp":"invalid"}', '{"timestamp":null}'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                cfg = config(tmp)
                cfg.path.parent.mkdir(parents=True)
                prior = {"type": "CallTranscript", "timestamp": 1_700_000_000, "callHash": "previous"}
                original = json.dumps(prior) + "\\n" + invalid + "\\n"
                cfg.path.write_text(original, encoding="utf-8")
                next_record = {"type": "CallTranscript", "timestamp": 1_700_000_010, "callHash": "next"}
                with self.assertRaisesRegex(ValueError, "invalid existing voice record"):
                    T.store_record(cfg, next_record, now=1_700_000_010)
                self.assertEqual(cfg.path.read_text(encoding="utf-8"), original)

    def test_corrupt_retention_state_reports_failure_without_interrupting_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript, _ = call(tmp)
            transcript.add_turn("caller", "please help")
            transcript.mark_disclosure_played()
            path = config(tmp).path
            path.parent.mkdir(parents=True)
            path.write_text('{"timestamp":false}\\n', encoding="utf-8")
            failures = []
            with self.assertLogs("model-garden.call-transcript", level="ERROR"):
                self.assertIsNone(T.finalize(transcript, on_failure=failures.append))
            self.assertEqual(failures, ["ValueError"])
            self.assertEqual(path.read_text(encoding="utf-8"), '{"timestamp":false}\\n')

    def test_expired_rows_are_dropped_on_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp, retention_seconds=3600)
            cfg.path.parent.mkdir(parents=True)
            old = {"type": "CallTranscript", "timestamp": 1_000, "callHash": "old"}
            fresh = {"type": "CallTranscript", "timestamp": 1_700_000_000 - 10, "callHash": "fresh"}
            cfg.path.write_text("\n".join(json.dumps(item) for item in (old, fresh)) + "\n", encoding="utf-8")
            record = {"type": "CallTranscript", "timestamp": 1_700_000_000, "callHash": "new"}
            self.assertTrue(T.store_record(cfg, record, now=1_700_000_000))
            hashes = [json.loads(line)["callHash"] for line in cfg.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(hashes, ["fresh", "new"])

    def test_storage_failure_never_raises_and_reports_only_the_error_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = pathlib.Path(tmp) / "blocker"
            blocker.write_text("a file where a directory is needed", encoding="utf-8")
            transcript, _ = call(tmp, path=blocker / "transcripts.jsonl")
            transcript.add_turn("caller", f"secret {VISA} text")
            transcript.mark_disclosure_played()
            failures: list[str] = []
            with self.assertLogs("model-garden.call-transcript", level="ERROR") as logs:
                result = T.finalize(transcript, on_failure=failures.append)
        self.assertIsNone(result)
        self.assertEqual(len(failures), 1)
        self.assertRegex(failures[0], r"^\w+Error$")
        self.assertNotIn("secret", " ".join(logs.output) + failures[0])

    def test_failing_failure_callback_cannot_break_the_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = pathlib.Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            transcript, _ = call(tmp, path=blocker / "transcripts.jsonl")

            def explode(_: str) -> None:
                raise RuntimeError("ledger unavailable")

            transcript.mark_disclosure_played()
            with self.assertLogs("model-garden.call-transcript", level="ERROR"):
                self.assertIsNone(T.finalize(transcript, on_failure=explode))

    def test_disabled_or_suppressed_transcripts_record_and_store_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            off, _ = call(tmp, enabled=False)
            self.assertIsNone(off.disclosure_text)
            off.add_turn("caller", "hello")
            self.assertIsNone(T.finalize(off))
            suppressed, _ = call(tmp)
            suppressed.add_turn("caller", "before")
            suppressed.suppress()
            suppressed.add_turn("caller", "after")
            self.assertIsNone(T.finalize(suppressed))
            self.assertFalse(config(tmp).path.exists())


class WorkerWiringTests(unittest.TestCase):
    """LiveKit is not installed in CI, so assert the worker's static shape instead of running it."""

    def setUp(self) -> None:
        self.source = (ROOT / "platform" / "channels" / "livekit_receptionist.py").read_text(encoding="utf-8")

    def test_worker_parses_and_uses_the_transcript_module(self):
        ast.parse(self.source)
        for needle in (
            "from call_transcript import",
            "transcript_config_from_env()",
            'session.on("conversation_item_added", record_turn)',
            "allow_interruptions=False",
            "mark_disclosure_played()",
            "self._transcript.suppress()",
            "finalize_transcript(transcript, on_failure=failed, auto_summary=True)",
            'getattr(item, "transcript_confidence", None)',
            "TranscriptWriteFailed",
            'note_transfer()',
            'note_outcome("message-taken")',
        ):
            self.assertIn(needle, self.source)

    def test_worker_never_logs_or_ledgers_transcript_text(self):
        self.assertNotIn("LOGGER.error(f", self.source)
        self.assertNotIn("transcript.build(", self.source)


class ExtractiveSummaryTests(unittest.TestCase):
    """A deterministic call summary that is labelled as such (#107)."""

    def transcript(self, *turns, **kwargs):
        config = T.TranscriptConfig(enabled=True, path=pathlib.Path("/nonexistent/t.jsonl"), disclosure="This call is recorded.", override_owner=None,
                                    retention_seconds=86400, patterns=())
        transcript = T.CallTranscript("abc123", "receptionist", config, started=1_800_000_000, **kwargs)
        for speaker, text in turns:
            transcript.add_turn(speaker, text)
        return transcript

    def test_the_summary_quotes_the_first_caller_turn_the_last_agent_turn_counts_and_outcome(self):
        transcript = self.transcript(("caller", "Hi, are you open on Saturday?"), ("agent", "We open at 9."), ("caller", "Great, book me in."), ("agent", "Booked for 10am."))
        record = transcript.build(auto_summary=True)
        self.assertEqual(record["summarySource"], "extractive")
        self.assertIn('The caller first said: "Hi, are you open on Saturday?"', record["summary"])
        self.assertIn('The agent\'s last reply: "Booked for 10am."', record["summary"])
        self.assertIn("2 caller and 2 agent turns. Outcome: resolved.", record["summary"])

    def test_it_is_off_unless_asked_for_and_a_supplied_summary_always_wins(self):
        transcript = self.transcript(("caller", "Hello"), ("agent", "Hi"))
        self.assertNotIn("summary", transcript.build())
        supplied = transcript.build(summary="Caller asked about hours; booked Friday.", auto_summary=True)
        self.assertEqual((supplied["summary"], supplied["summarySource"]), ("Caller asked about hours; booked Friday.", "provided"))

    def test_long_turns_are_truncated_and_card_numbers_never_reach_the_summary(self):
        transcript = self.transcript(("caller", "My card is 4111 1111 1111 1111 " + "and more " * 60), ("agent", "x" * 400))
        record = transcript.build(auto_summary=True)
        self.assertNotIn("4111", record["summary"])
        self.assertLess(len(record["summary"]), 600)
        self.assertIn("…", record["summary"])

    def test_a_call_with_no_speech_says_so_and_is_deterministic(self):
        self.assertEqual(self.transcript().build(auto_summary=True)["summary"], "No speech was recorded. Outcome: abandoned.")
        one = self.transcript(("caller", "Hello"), ("agent", "Hi")).build(auto_summary=True, ended=1)
        two = self.transcript(("caller", "Hello"), ("agent", "Hi")).build(auto_summary=True, ended=1)
        self.assertEqual(one, two)

    def test_the_observed_outcome_is_used(self):
        transcript = self.transcript(("caller", "I want a person"))
        transcript.note_outcome("transferred")
        self.assertIn("Outcome: transferred.", transcript.build(auto_summary=True)["summary"])

    def test_finalize_passes_the_option_through(self):
        transcript = self.transcript(("caller", "Hello"), ("agent", "Hi"))
        with tempfile.TemporaryDirectory() as tmp:
            transcript.config = T.TranscriptConfig(enabled=True, path=pathlib.Path(tmp) / "t.jsonl", disclosure="This call is recorded.", override_owner=None,
                                                   retention_seconds=86400, patterns=())
            transcript.mark_disclosure_played()
            record = T.finalize(transcript, auto_summary=True)
        self.assertEqual(record["summarySource"], "extractive")


if __name__ == "__main__":
    unittest.main()
