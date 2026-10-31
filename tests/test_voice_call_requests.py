"""Credential-free checks for ledger request-id attribution on stored calls and the voice overlay's worker mount."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


requests = _load("call_requests_under_test", "platform/channels/call_requests.py")
voice = _load("deploy_reference_voice_under_test", "scripts/deploy-reference-voice.py")
START = 1_760_000_000


def iso(seconds: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def event(at: int, request_id: str, *, kind="authorization", user="runtime:hermes", correlation=None) -> dict:
    row = {"timestamp": iso(at), "event": kind, "requestId": request_id, "initiatingUser": user}
    if correlation is not None:
        row["governance"] = {"correlationId": correlation}
    return row


def call(start: int, end: int, **extra) -> dict:
    return {"type": "CallTranscript", "callHash": f"{start:064x}", "timestamp": start, "endedAt": end, **extra}


class RequestIdTests(unittest.TestCase):
    def test_runtime_requests_inside_the_window_are_attached_in_order_without_duplicates(self):
        events = [event(START + 5, "r1"), event(START + 6, "r1", kind="execution"), event(START + 9, "r2"), event(START + 500, "late")]
        (record,) = requests.attach_request_ids([call(START, START + 30)], events)
        self.assertEqual((record["requestIds"], record["requestIdsSource"]), (["r1", "r2"], "ledger-time-window"))

    def test_other_people_and_other_identities_are_not_attributed_to_the_call(self):
        events = [event(START + 5, "mine"), event(START + 6, "pat", user="pat@acme.example"), event(START + 7, "mail", user="runtime:support-mailbox")]
        (record,) = requests.attach_request_ids([call(START, START + 30)], events)
        self.assertEqual(record["requestIds"], ["mine"])

    def test_overlapping_calls_get_no_ids_because_the_ledger_cannot_say_which(self):
        records = requests.attach_request_ids([call(START, START + 60), call(START + 30, START + 90), call(START + 1000, START + 1030)],
                                              [event(START + 40, "x"), event(START + 1010, "y")])
        self.assertEqual([item.get("requestIds") for item in records], [None, None, ["y"]])

    def test_unverified_stored_ids_cannot_override_ledger_and_inputs_are_not_changed(self):
        original = call(START, START + 30, requestIds=["forged"], requestIdsSource="ledger-correlation")
        plain = call(START + 500, START + 530)
        records = requests.attach_request_ids([original, plain], [event(START + 5, "real")])
        self.assertEqual((records[0]["requestIds"], records[0]["requestIdsSource"]),
                         (["real"], "ledger-time-window"))
        self.assertEqual(original["requestIds"], ["forged"])
        self.assertEqual(original["requestIdsSource"], "ledger-correlation")
        self.assertNotIn("requestIds", plain)

    def test_stored_request_ids_are_not_proof_without_current_ledger_authorizations(self):
        record = call(START, START + 30, requestIds=["forged"], requestIdsSource="ledger-correlation")
        (result,) = requests.attach_request_ids([record], [])
        self.assertNotIn("requestIds", result)
        self.assertNotIn("requestIdsSource", result)
        self.assertEqual(record["requestIds"], ["forged"])

    def test_stored_request_ids_cannot_bypass_conflicting_or_task_scoped_ledger(self):
        record = call(START, START + 30, requestIds=["shared"], requestIdsSource="ledger-correlation")
        tag = "call:" + record["callHash"]
        for ledger in (
            [event(START + 5, "shared", correlation=tag),
             event(START + 6, "shared", correlation="task:other")],
            [event(START + 5, "shared", correlation="task:other")],
            [event(START + 5, "shared", correlation=tag),
             event(START + 6, "shared", user="runtime:mailbox", correlation=tag)],
        ):
            with self.subTest(ledger=ledger):
                (result,) = requests.attach_request_ids([record], ledger)
                self.assertNotIn("requestIds", result)
                self.assertNotIn("requestIdsSource", result)

    def test_stored_request_ids_are_recomputed_from_exact_ledger_evidence(self):
        record = call(START, START + 30, requestIds=["fake"], requestIdsSource="ledger-time-window")
        tag = "call:" + record["callHash"]
        (result,) = requests.attach_request_ids([record], [event(START + 5, "actual", correlation=tag)])
        self.assertEqual((result["requestIds"], result["requestIdsSource"]),
                         (["actual"], "ledger-correlation"))

    def test_reconciliation_is_idempotent_and_rejects_stale_ledger_claims(self):
        record = call(START, START + 30)
        tag = "call:" + record["callHash"]
        ledger = [event(START + 5, "real", correlation=tag)]
        (first,) = requests.attach_request_ids([record], ledger)
        (again,) = requests.attach_request_ids([first], ledger)
        self.assertEqual(first, again)
        (stale,) = requests.attach_request_ids([again], [])
        self.assertNotIn("requestIds", stale)
        self.assertNotIn("requestIdsSource", stale)
        self.assertEqual(first["requestIds"], ["real"])

    def test_a_call_without_a_usable_window_or_without_requests_is_left_alone(self):
        (empty,) = requests.attach_request_ids([call(START, START + 30)], [])
        self.assertNotIn("requestIds", empty)
        (broken,) = requests.attach_request_ids([{"type": "CallTranscript", "callHash": "a"}], [event(START, "r1")])
        self.assertNotIn("requestIds", broken)


    def test_explicit_call_correlation_attributes_overlapping_calls_without_time_guessing(self):
        first = call(START, START + 60)
        second = call(START + 30, START + 90)
        first_tag, second_tag = "call:" + first["callHash"], "call:" + second["callHash"]
        events = [
            event(START + 40, "first", correlation=first_tag),
            event(START + 41, "second", user="verified:staff", correlation=second_tag),
            event(START + 42, "background"),
        ]
        records = requests.attach_request_ids([first, second], events)
        self.assertEqual(records[0]["requestIds"], ["first"])
        self.assertEqual(records[1]["requestIds"], ["second"])
        self.assertEqual([record["requestIdsSource"] for record in records], ["ledger-correlation"] * 2)
        self.assertNotIn("background", records[0]["requestIds"] + records[1]["requestIds"])

    def test_exact_correlation_outside_call_window_is_accepted_but_other_trace_is_not_guessed(self):
        record = call(START, START + 20)
        tagged = event(START + 300, "late", correlation="call:" + record["callHash"])
        unrelated = event(START + 5, "other-task", correlation="task:unrelated")
        (result,) = requests.attach_request_ids([record], [tagged, unrelated])
        self.assertEqual((result["requestIds"], result["requestIdsSource"]), (["late"], "ledger-correlation"))
        (unrelated_only,) = requests.attach_request_ids([record], [unrelated])
        self.assertNotIn("requestIds", unrelated_only)

    def test_a_request_id_tagged_to_two_different_calls_is_ambiguous_and_not_attached(self):
        first, second = call(START, START + 20), call(START + 60, START + 80)
        events = [
            event(START + 5, "reused", correlation="call:" + first["callHash"]),
            event(START + 65, "reused", correlation="call:" + second["callHash"]),
        ]
        records = requests.attach_request_ids([first, second], events)
        self.assertTrue(all("requestIds" not in record for record in records))

    def test_invalid_correlations_and_naive_timestamps_cannot_create_attribution(self):
        record = call(START, START + 20)
        forged = event(START + 5, "forged", correlation="call:" + record["callHash"].upper())
        malformed = event(START + 6, "malformed", correlation=123)
        naive = event(START + 7, "naive")
        naive["timestamp"] = "2026-01-01T00:00:00"
        records = requests.attach_request_ids([record], [forged, malformed, naive])
        self.assertNotIn("requestIds", records[0])

    def test_explicit_attribution_revalidates_existing_ids_and_deduplicates_authorizations(self):
        record = call(START, START + 10)
        tagged = event(START + 5, "a", correlation="call:" + record["callHash"])
        (result,) = requests.attach_request_ids([record], [tagged, tagged, event(START + 6, "b", correlation=tagged["governance"]["correlationId"])])
        self.assertEqual(result["requestIds"], ["a", "b"])
        self.assertEqual(result["requestIdsSource"], "ledger-correlation")
        (recomputed,) = requests.attach_request_ids([dict(record, requestIds=["unverified"])], [tagged])
        self.assertEqual(recomputed["requestIds"], ["a"])
        self.assertEqual(recomputed["requestIdsSource"], "ledger-correlation")
        self.assertNotIn("requestIds", record)



    def test_untagged_duplicate_cannot_override_an_unrelated_task_tag(self):
        record = call(START, START + 30)
        events = [event(START + 5, "reused"), event(START + 6, "reused", correlation="task:other")]
        (result,) = requests.attach_request_ids([record], events)
        self.assertNotIn("requestIds", result)

    def test_exact_call_tag_is_rejected_when_request_is_also_tagged_to_a_task(self):
        record = call(START, START + 30)
        tag = "call:" + record["callHash"]
        events = [event(START + 5, "reused", correlation=tag),
                  event(START + 6, "reused", correlation="task:other")]
        (result,) = requests.attach_request_ids([record], events)
        self.assertNotIn("requestIds", result)

    def test_blank_or_task_scoped_governance_is_not_untagged_voice_work(self):
        record = call(START, START + 30)
        blank = event(START + 5, "blank", correlation="")
        task = event(START + 6, "task")
        task["governance"] = {"taskId": "task-elsewhere"}
        (result,) = requests.attach_request_ids([record], [blank, task])
        self.assertNotIn("requestIds", result)

    def test_malformed_correlation_on_duplicate_vetoes_exact_attribution(self):
        record = call(START, START + 30)
        tagged = event(START + 5, "reused", correlation="call:" + record["callHash"])
        malformed = event(START + 6, "reused", correlation=123)
        (result,) = requests.attach_request_ids([record], [tagged, malformed])
        self.assertNotIn("requestIds", result)

    def test_conflicting_event_with_invalid_timestamp_still_vetoes_attribution(self):
        record = call(START, START + 30)
        tagged = event(START + 5, "reused", correlation="call:" + record["callHash"])
        conflicting = event(START + 6, "reused", correlation="task:elsewhere")
        conflicting["timestamp"] = "not-a-timestamp"
        (result,) = requests.attach_request_ids([record], [tagged, conflicting])
        self.assertNotIn("requestIds", result)


    def test_same_exact_request_id_with_conflicting_actors_is_not_attributed(self):
        record = call(START, START + 30)
        tag = "call:" + record["callHash"]
        legitimate = event(START + 5, "shared", correlation=tag)
        other = event(START + 6, "shared", user="runtime:mailbox", correlation=tag)
        other["timestamp"] = "invalid"  # conflicts must veto before timestamp filtering
        (result,) = requests.attach_request_ids([record], [legitimate, other])
        self.assertNotIn("requestIds", result)

    def test_untagged_request_reused_by_another_actor_cannot_be_guessed(self):
        record = call(START, START + 30)
        events = [event(START + 5, "shared"), event(START + 6, "shared", user="pat@client.example")]
        (result,) = requests.attach_request_ids([record], events)
        self.assertNotIn("requestIds", result)

    def test_exact_request_without_a_verified_actor_is_not_attributed(self):
        record = call(START, START + 30)
        tagged = event(START + 5, "missing-actor", correlation="call:" + record["callHash"])
        tagged.pop("initiatingUser")
        (result,) = requests.attach_request_ids([record], [tagged])
        self.assertNotIn("requestIds", result)

    def test_same_actor_duplicate_authorizations_still_deduplicate(self):
        record = call(START, START + 30)
        tag = "call:" + record["callHash"]
        events = [event(START + 5, "same", user="verified:staff", correlation=tag),
                  event(START + 6, "same", user="verified:staff", correlation=tag)]
        (result,) = requests.attach_request_ids([record], events)
        self.assertEqual((result["requestIds"], result["requestIdsSource"]), (["same"], "ledger-correlation"))


class OverlayTests(unittest.TestCase):
    def test_the_worker_reads_the_voice_volume_read_only(self):
        overlay = voice.worker_overlay({"services": {"hermes": {}, "governed-worker": {}}})
        self.assertEqual(overlay["volumes"], ["voice-data:/opt/voice:ro"])
        self.assertEqual(overlay["environment"], {"MODEL_GARDEN_VOICE_TRANSCRIPTS": "/opt/voice/transcripts.jsonl",
                                                  "MODEL_GARDEN_VOICE_AGENT_NAME": "model-garden-receptionist"})

    def test_voice_worker_mount_is_bound_to_exact_deployed_agent(self):
        overlay = voice.worker_overlay({"services": {"governed-worker": {}}}, agent_name="client-receptionist")
        self.assertEqual(overlay["environment"]["MODEL_GARDEN_VOICE_AGENT_NAME"], "client-receptionist")
        for invalid in ("", " ", "../other", "agent/name"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "voice agent"):
                voice.worker_overlay({"services": {"governed-worker": {}}}, agent_name=invalid)

    def test_no_worker_in_the_base_means_nothing_is_added(self):
        self.assertIsNone(voice.worker_overlay({"services": {"hermes": {}}}))
        self.assertIsNone(voice.worker_overlay({}))


if __name__ == "__main__":
    unittest.main()
