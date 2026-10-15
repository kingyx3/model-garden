"""Credential-free checks for the ledger export delivery alarms (lag and stale head manifest)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
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


govern = _load("govern_action_for_lag", "scripts/govern-action.py")
export = _load("ledger_export_for_lag", "platform/core/ledger_export.py")
cli = _load("ledger_export_cli_for_lag", "scripts/ledger-export.py")

NOW = 1_790_000_000


def write_events(path: pathlib.Path, first: int, count: int) -> None:
    for index in range(first, first + count):
        request = {"requestId": f"{index:064x}", "agent": "receptionist", "tool": "calendar.availability", "arguments": {"n": index}}
        govern.append_audit_event(path, govern.make_audit_event("authorization", request, outcome="allow"))


class LagCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.ledger, self.state = root / "audit.jsonl", root / "state.json"
        self.sink = export.DirectorySink(root / "sink")

    def export(self, *, exported_at: int = NOW) -> None:
        export.export_ledger(self.ledger, self.sink, self.state, exported_at=exported_at)

    def verify(self, **kwargs) -> dict:
        return export.verify_export(self.ledger, self.sink, now=NOW + kwargs.pop("after", 0), **kwargs)


class LagTests(LagCase):
    def test_a_fully_exported_idle_ledger_never_alarms_however_old_the_head_is(self):
        write_events(self.ledger, 1, 3)
        self.export(exported_at=NOW - 10 * 86400)
        report = self.verify(max_lag_events=0, max_head_age_seconds=60)
        self.assertEqual((report["ok"], report["lagEvents"], report["headAgeSeconds"]), (True, 0, 10 * 86400))

    def test_alarms_are_off_unless_asked_for(self):
        write_events(self.ledger, 1, 3)
        self.export()
        write_events(self.ledger, 4, 50)
        report = self.verify(after=10 * 86400)
        self.assertTrue(report["ok"])
        self.assertEqual(report["lagEvents"], 50)

    def test_a_ledger_too_far_ahead_of_the_export_is_reported_with_the_numbers(self):
        write_events(self.ledger, 1, 3)
        self.export()
        write_events(self.ledger, 4, 5)
        self.assertTrue(self.verify(max_lag_events=5)["ok"])
        report = self.verify(max_lag_events=4)
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"], ["export lag: the local ledger is 5 event(s) ahead of the export (limit 4)"])

    def test_waiting_events_with_an_old_head_manifest_mean_the_exporter_stalled(self):
        write_events(self.ledger, 1, 3)
        self.export(exported_at=NOW)
        write_events(self.ledger, 4, 1)
        self.assertTrue(self.verify(after=3600, max_head_age_seconds=3600)["ok"])
        report = self.verify(after=3601, max_head_age_seconds=3600)
        self.assertFalse(report["ok"])
        self.assertIn("export stale: events are waiting and the newest head manifest is 3601 seconds old (limit 3600)", report["problems"])

    def test_events_with_nothing_ever_exported_is_stale_not_silent(self):
        write_events(self.ledger, 1, 2)
        report = self.verify(max_head_age_seconds=60)
        self.assertFalse(report["ok"])
        self.assertIn("export stale: events are waiting and no head manifest has ever been exported", report["problems"])

    def test_truncation_is_reported_once_and_not_as_negative_lag(self):
        write_events(self.ledger, 1, 4)
        self.export()
        self.ledger.write_text("\n".join(self.ledger.read_text().splitlines()[:2]) + "\n", encoding="utf-8")
        report = self.verify(max_lag_events=0)
        self.assertEqual(report["lagEvents"], 0)
        self.assertTrue(report["truncated"])
        self.assertFalse(any("lag" in problem for problem in report["problems"]))

    def test_thresholds_must_be_non_negative_integers(self):
        write_events(self.ledger, 1, 1)
        for bad in (-1, 1.5, "10", True):
            with self.assertRaises(ValueError):
                self.verify(max_lag_events=bad)
            with self.assertRaises(ValueError):
                self.verify(max_head_age_seconds=bad)


class CommandTests(LagCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_command_exits_non_zero_with_an_alert_line_for_a_stalled_exporter(self):
        write_events(self.ledger, 1, 3)
        self.export(exported_at=1)  # the head manifest is decades old
        write_events(self.ledger, 4, 2)
        base = ("verify", "--ledger", str(self.ledger), "--sink-dir", str(self.sink.root))
        code, out, err = self.run_cli(*base)
        self.assertEqual((code, json.loads(out)["lagEvents"], err), (0, 2, ""))
        code, _, err = self.run_cli(*base, "--max-lag-events", "1")
        self.assertEqual(code, 2)
        self.assertIn("ALERT: export lag", err)
        code, _, err = self.run_cli(*base, "--max-head-age-seconds", "3600")
        self.assertEqual(code, 2)
        self.assertIn("ALERT: export stale", err)

    def test_an_invalid_threshold_is_an_error_not_an_alert(self):
        write_events(self.ledger, 1, 1)
        self.export()
        code, _, err = self.run_cli("verify", "--ledger", str(self.ledger), "--sink-dir", str(self.sink.root), "--max-lag-events", "-3")
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", err)


if __name__ == "__main__":
    unittest.main()
