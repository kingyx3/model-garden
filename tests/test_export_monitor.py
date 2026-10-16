"""Credential-free checks that ledger-export problems become one alert per condition, reminders, and a recovery notice (#80)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


govern = _load("govern_for_monitor", "scripts/govern-action.py")
export = _load("ledger_export_for_monitor", "platform/core/ledger_export.py")
monitor_module = _load("export_monitor_under_test", "platform/core/export_monitor.py")
delivery = _load("notice_delivery_for_monitor", "platform/core/notice_delivery.py")
NOW = 1_790_000_000
HOUR = 3600
OWNER, BACKUP = "owner@client.example", "backup@client.example"
SECRET_ARGUMENT = "Hargreaves boiler at 12 Acacia Road"


def write_events(path: pathlib.Path, first: int, count: int) -> None:
    for index in range(first, first + count):
        request = {"requestId": f"{index:064x}", "agent": "receptionist", "tool": "calendar.availability", "arguments": {"n": index, "note": SECRET_ARGUMENT}}
        govern.append_audit_event(path, govern.make_audit_event("authorization", request, outcome="allow"))


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.ledger, self.state = root / "audit.jsonl", root / "state.json"
        self.sink = export.DirectorySink(root / "sink")
        self.events: list[tuple] = []
        self.monitor = monitor_module.ExportMonitor(root / "monitor", audit=lambda *event: self.events.append(event))

    def export(self, at=NOW):
        export.export_ledger(self.ledger, self.sink, self.state, exported_at=at)

    def check(self, at=NOW, **kwargs):
        values = dict(now=at, owner=OWNER, max_lag_events=2, max_head_age_seconds=HOUR)
        values.update(kwargs)
        return self.monitor.check(self.ledger, self.sink, **values)

    def deliver(self, result, at):
        notices = result["notices"]
        source = delivery.source("export", notices, lambda item, via: self.monitor.mark_notified(item["notice"], via=via, now=at), reference=lambda n: n["key"][:12])
        directory = delivery.Directory({OWNER: {"channel": "inbox"}, BACKUP: {"channel": "inbox"}})
        return delivery.deliver([source], directory, {})


class ClassificationTests(unittest.TestCase):
    def test_problem_lines_map_to_conditions(self):
        classify = monitor_module.classify
        self.assertEqual(classify("export lag: the local ledger is 9 event(s) ahead of the export (limit 2)"), "export-lag")
        self.assertEqual(classify("export stale: events are waiting and the newest head manifest is 90 seconds old (limit 60)"), "export-stale")
        for line in ("local ledger truncated: it ends at sequence 3", "export gap: missing sequence(s) [2]", "local ledger differs from the export at sequence 4",
                     "head manifest at sequence 5 does not match the exported record", "no head manifest has been exported", "local ledger invalid: broken"):
            self.assertEqual(classify(line), "export-integrity", line)


class HealthyTests(Case):
    def test_a_current_export_and_an_idle_ledger_raise_nothing(self):
        write_events(self.ledger, 1, 3)
        self.export(at=NOW - 10 * 86400)
        result = self.check(at=NOW)
        self.assertEqual((result["report"]["ok"], result["notices"]), (True, []))
        self.assertEqual(self.events, [])

    def test_an_export_that_was_never_set_up_has_nothing_to_compare_and_raises_nothing(self):
        self.assertEqual(self.check(at=NOW)["notices"], [])  # no ledger events and an empty sink

    def test_recovery_is_not_announced_for_something_that_never_failed(self):
        write_events(self.ledger, 1, 3)
        self.export()
        for step in range(3):
            self.assertEqual(self.check(at=NOW + step)["notices"], [])


class LagTests(Case):
    def check(self, at=NOW, **kwargs):  # only the lag alarm is under test here, not the head-manifest age
        kwargs.setdefault("max_head_age_seconds", None)
        return super().check(at, **kwargs)

    def lagging(self):
        write_events(self.ledger, 1, 3)
        self.export()
        write_events(self.ledger, 4, 5)

    def test_lag_alerts_once_and_names_the_condition_the_size_and_a_recovery_step_but_no_ledger_content(self):
        self.lagging()
        (notice,) = self.check(at=NOW + 10)["notices"]
        self.assertEqual((notice["kind"], notice["to"]), ("export-lag", OWNER))
        self.assertIn("5 event(s) behind", notice["text"])
        self.assertIn("export job", notice["text"])
        text = json.dumps(notice)
        self.assertNotIn(SECRET_ARGUMENT, text)
        self.assertNotIn(str(self.ledger), text)
        self.assertEqual([event[0] for event in self.events], ["export.alert-started"])

    def test_an_undelivered_notice_is_offered_again_and_a_delivered_one_waits_for_the_reminder(self):
        self.lagging()
        first = self.check(at=NOW + 10)
        self.assertEqual(len(self.check(at=NOW + 20)["notices"]), 1)  # not marked: offered again
        results = self.deliver(first, NOW + 30)
        self.assertEqual([item["status"] for item in results], ["delivered"])
        self.assertEqual(self.check(at=NOW + 40)["notices"], [])
        self.assertEqual(self.check(at=NOW + 5 * HOUR)["notices"], [])
        reminder = self.check(at=NOW + 6 * HOUR + 31)["notices"]
        self.assertEqual(len(reminder), 1)
        self.assertIn("reminder", reminder[0]["text"])
        self.assertEqual(self.events[-1][0], "export.alert-delivered")

    def test_state_survives_a_restart_so_an_alert_is_not_repeated(self):
        self.lagging()
        self.deliver(self.check(at=NOW + 10), NOW + 11)
        again = monitor_module.ExportMonitor(self.monitor.root)
        self.assertEqual(again.check(self.ledger, self.sink, now=NOW + 60, owner=OWNER, max_lag_events=2, max_head_age_seconds=HOUR)["notices"], [])
        self.assertEqual(oct(self.monitor.path.stat().st_mode & 0o777), "0o600")

    def test_fixing_it_gives_exactly_one_recovery_notice(self):
        self.lagging()
        self.deliver(self.check(at=NOW + 10), NOW + 11)
        self.export(at=NOW + 100)
        recovered = self.check(at=NOW + 120)
        (notice,) = recovered["notices"]
        self.assertEqual(notice["kind"], "export-recovered")
        self.assertIn("healthy again", notice["text"])
        self.assertEqual(self.check(at=NOW + 125)["notices"][0]["kind"], "export-recovered")  # offered until delivered
        self.deliver(recovered, NOW + 130)
        self.assertEqual(self.check(at=NOW + 140)["notices"], [])

    def test_a_relapse_after_recovery_alerts_again(self):
        self.lagging()
        self.deliver(self.check(at=NOW + 10), NOW + 11)
        self.export(at=NOW + 100)
        self.deliver(self.check(at=NOW + 120), NOW + 121)
        write_events(self.ledger, 9, 5)
        notices = self.check(at=NOW + 200)["notices"]
        self.assertEqual([item["kind"] for item in notices], ["export-lag"])


class StaleAndIntegrityTests(Case):
    def test_waiting_events_with_an_old_head_manifest_alert_as_stale(self):
        write_events(self.ledger, 1, 3)
        self.export(at=NOW - 3 * HOUR)
        write_events(self.ledger, 4, 1)
        kinds = [item["kind"] for item in self.check(at=NOW, max_lag_events=100)["notices"]]
        self.assertEqual(kinds, ["export-stale"])

    def test_a_tampered_or_truncated_export_is_an_integrity_alert_that_says_it_cannot_be_trusted(self):
        write_events(self.ledger, 1, 4)
        self.export()
        write_events(self.ledger, 5, 1)
        victim = sorted((self.sink.root / "records").glob("*.json"))[1]
        victim.chmod(0o600)
        document = json.loads(victim.read_text())
        document["recordId"] = "0" * 64
        victim.write_text(json.dumps(document))
        result = self.check(at=NOW + 5)
        kinds = [item["kind"] for item in result["notices"]]
        self.assertIn("export-integrity", kinds)
        integrity = next(item for item in result["notices"] if item["kind"] == "export-integrity")
        self.assertIn("cannot be trusted", integrity["text"])
        self.assertFalse(result["report"]["ok"])

    def test_two_conditions_at_once_are_two_alerts_and_clear_independently(self):
        report = {"problems": ["export lag: the local ledger is 9 event(s) ahead of the export (limit 2)", "export gap: missing sequence(s) [3]"], "lagEvents": 9}
        notices = self.monitor.evaluate(report, now=NOW, owner=OWNER)
        self.assertEqual(sorted(item["kind"] for item in notices), ["export-integrity", "export-lag"])
        for item in notices:
            self.monitor.mark_notified(item, via="inbox", now=NOW + 1)
        only_lag = self.monitor.evaluate({"problems": ["export lag: the local ledger is 9 event(s) ahead of the export (limit 2)"], "lagEvents": 9}, now=NOW + 2, owner=OWNER)
        self.assertEqual(only_lag, [])  # the integrity condition cleared without a recovery notice while lag remains
        cleared = self.monitor.evaluate({"problems": [], "lagEvents": 0}, now=NOW + 3, owner=OWNER)
        self.assertEqual([item["kind"] for item in cleared], ["export-recovered"])


class OwnerAndInputTests(Case):
    def test_with_no_owner_the_alert_is_unowned_and_stays_loud_in_delivery(self):
        write_events(self.ledger, 1, 3)
        self.export()
        write_events(self.ledger, 4, 5)
        result = self.check(at=NOW + 10, owner=None)
        directory = delivery.Directory({OWNER: {"channel": "inbox"}})
        source = delivery.source("export", result["notices"], lambda item, via: None, reference=lambda n: n["key"][:12])
        for _ in range(2):
            self.assertEqual([item["status"] for item in delivery.deliver([source], directory, {})], ["unowned"])

    def test_the_recovery_goes_to_the_escalation_contact_when_there_is_no_owner(self):
        notices = self.monitor.evaluate({"problems": ["export lag: x"], "lagEvents": 1}, now=NOW, owner=None, escalate_to=BACKUP)
        self.assertIsNone(notices[0]["to"])
        recovered = self.monitor.evaluate({"problems": []}, now=NOW + 1, owner=None, escalate_to=BACKUP)
        self.assertEqual(recovered[0]["to"], BACKUP)

    def test_bad_inputs_are_refused(self):
        for kwargs in ({"owner": "not valid!"}, {"escalate_to": ""}, {"reminder_seconds": 5}, {"reminder_seconds": True}):
            values = dict(now=NOW, owner=OWNER)
            values.update(kwargs)
            with self.assertRaises(ValueError, msg=str(kwargs)):
                self.monitor.evaluate({"problems": []}, **values)
        for name in ("Bad Name", "", "a/b"):
            with self.assertRaises(ValueError):
                monitor_module.ExportMonitor(pathlib.Path(self._tmp.name), name=name)
        with self.assertRaises(ValueError):
            self.monitor.mark_notified({"condition": "export-lag"}, via="Bad Channel", now=NOW)

    def test_a_notice_for_a_cleared_condition_cannot_re_arm_it(self):
        self.monitor.mark_notified({"condition": "export-lag"}, via="inbox", now=NOW)  # nothing active: a no-op, not an error
        self.assertEqual(self.monitor.evaluate({"problems": []}, now=NOW + 1, owner=OWNER), [])


if __name__ == "__main__":
    unittest.main()
