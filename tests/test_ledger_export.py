from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


govern = _load("govern_action_for_export", "scripts/govern-action.py")
export = _load("ledger_export_under_test", "platform/core/ledger_export.py")
cli = _load("ledger_export_cli", "scripts/ledger-export.py")


def request(index: int) -> dict:
    return {
        "requestId": f"{index:064x}",
        "agent": "receptionist",
        "tool": "calendar.availability",
        "arguments": {"start": f"2026-10-{index:02d}T10:00:00+08:00"},
    }


def write_ledger(path: pathlib.Path, count: int, *, start: int = 1) -> None:
    for index in range(start, start + count):
        govern.append_audit_event(path, govern.make_audit_event("authorization", request(index), outcome="allow"))


class Workspace:
    def __init__(self, tmp: str) -> None:
        self.root = pathlib.Path(tmp)
        self.ledger = self.root / "audit.jsonl"
        self.state = self.root / "state" / "export.json"
        self.sink = export.DirectorySink(self.root / "sink")

    def export(self, **kwargs) -> dict:
        return export.export_ledger(self.ledger, self.sink, self.state, **kwargs)

    def lines(self) -> list[str]:
        return self.ledger.read_text(encoding="utf-8").splitlines()


class ExportTests(unittest.TestCase):
    def test_export_matches_the_governed_chain_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 5)
            summary = ws.export(exported_at=1_700_000_000)
            local = [json.loads(line) for line in ws.lines()]
            records = ws.sink.records()
            self.assertEqual(summary, {"exported": 5, "totalChainedEvents": 5, "headSequence": 5})
            self.assertEqual([record["recordId"] for record in records], [event["eventHash"] for event in local])
            self.assertEqual(ws.sink.heads()[-1]["headHash"], local[-1]["eventHash"])
            self.assertTrue(govern.verify_audit_chain(ws.ledger)["valid"])
            report = export.verify_export(ws.ledger, ws.sink)
            self.assertTrue(report["ok"], report["problems"])
            self.assertEqual((report["localChainedEvents"], report["exportedRecords"]), (5, 5))

    def test_export_is_incremental_and_rerunnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 3)
            ws.export(exported_at=1)
            self.assertEqual(ws.export(exported_at=2)["exported"], 0)
            write_ledger(ws.ledger, 2, start=4)
            self.assertEqual(ws.export(exported_at=3)["exported"], 2)
            self.assertEqual(len(ws.sink.records()), 5)
            self.assertTrue(export.verify_export(ws.ledger, ws.sink)["ok"])

    def test_batches_and_empty_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            self.assertEqual(ws.export()["exported"], 0)
            self.assertEqual(ws.sink.records(), [])
            write_ledger(ws.ledger, 7)
            self.assertEqual(ws.export(batch_size=3)["exported"], 7)
            with self.assertRaises(ValueError):
                ws.export(batch_size=0)

    def test_failed_batch_does_not_advance_the_cursor_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 4)

            class FlakySink(export.DirectorySink):
                calls = 0

                def put_records(self, records):
                    FlakySink.calls += 1
                    super().put_records(records)
                    if FlakySink.calls == 2:
                        raise export.LedgerExportError("sink went away after writing")

            flaky = FlakySink(ws.sink.root)
            with self.assertRaises(export.LedgerExportError):
                export.export_ledger(ws.ledger, flaky, ws.state, batch_size=2)
            self.assertEqual(json.loads(ws.state.read_text())["sequence"], 2)
            # At-least-once: the second batch landed but was not acknowledged; the retry re-sends it.
            self.assertEqual(ws.export(batch_size=2)["exported"], 2)
            self.assertEqual(len(ws.sink.records()), 4)
            self.assertTrue(export.verify_export(ws.ledger, ws.sink)["ok"])

    def test_sink_rejects_a_different_record_under_an_existing_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 1)
            ws.export()
            record = ws.sink.records()[0]
            ws.sink.put_records([record])  # identical re-delivery is a no-op
            forged = {**record, "event": {**record["event"], "outcome": "deny"}}
            with self.assertRaisesRegex(export.LedgerExportError, "different content"):
                ws.sink.put_records([forged])

    def test_sink_files_are_private_and_never_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 1)
            ws.export()
            for path in (ws.sink.root / "records").glob("*.json"):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_legacy_unchained_records_may_precede_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            ws.ledger.write_text(json.dumps({"event": "legacy", "requestId": "x"}) + "\n", encoding="utf-8")
            write_ledger(ws.ledger, 3)
            self.assertTrue(govern.verify_audit_chain(ws.ledger)["valid"])
            self.assertEqual(ws.export()["exported"], 3)
            records = ws.sink.records()
            self.assertEqual([record["sequence"] for record in records], [2, 3, 4])
            self.assertTrue(export.verify_export(ws.ledger, ws.sink)["ok"])


class TamperDetectionTests(unittest.TestCase):
    def exported(self, tmp: str, count: int = 5) -> Workspace:
        ws = Workspace(tmp)
        write_ledger(ws.ledger, count)
        ws.export(exported_at=10)
        return ws

    def test_truncating_the_local_log_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.exported(tmp)
            ws.ledger.write_text("\n".join(ws.lines()[:3]) + "\n", encoding="utf-8")
            self.assertTrue(govern.verify_audit_chain(ws.ledger)["valid"], "a truncated chain still self-verifies")
            report = export.verify_export(ws.ledger, ws.sink)
            self.assertFalse(report["ok"])
            self.assertTrue(report["truncated"])
            self.assertTrue(any("truncated" in problem for problem in report["problems"]))
            with self.assertRaises(export.LedgerTruncated):
                ws.export()

    def test_a_rewritten_local_log_is_detected_as_divergence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.exported(tmp, 3)
            ws.ledger.unlink()
            write_ledger(ws.ledger, 3, start=10)  # a different, internally valid chain of the same length
            self.assertTrue(govern.verify_audit_chain(ws.ledger)["valid"])
            report = export.verify_export(ws.ledger, ws.sink)
            self.assertFalse(report["ok"])
            self.assertTrue(any("differs from the export at sequence 1" in problem for problem in report["problems"]))
            with self.assertRaisesRegex(export.LedgerTruncated, "diverges"):
                ws.export()

    def test_a_missing_exported_record_is_reported_as_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.exported(tmp)
            next((ws.sink.root / "records").glob("000000000003-*.json")).unlink()
            report = export.verify_export(ws.ledger, ws.sink)
            self.assertFalse(report["ok"])
            self.assertEqual(report["gaps"], [3])
            self.assertTrue(any("export gap" in problem for problem in report["problems"]))

    def test_a_missing_first_record_is_reported_as_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.exported(tmp)
            next((ws.sink.root / "records").glob("000000000001-*.json")).unlink()
            self.assertEqual(export.verify_export(ws.ledger, ws.sink)["gaps"], [1])

    def test_an_altered_exported_record_fails_its_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.exported(tmp)
            path = next((ws.sink.root / "records").glob("000000000002-*.json"))
            record = json.loads(path.read_text())
            record["event"]["outcome"] = "deny"
            path.write_text(json.dumps(record), encoding="utf-8")
            report = export.verify_export(ws.ledger, ws.sink)
            self.assertFalse(report["ok"])
            self.assertTrue(any("fails its hash check" in problem for problem in report["problems"]))

    def test_head_manifest_must_match_the_exported_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.exported(tmp)
            path = next((ws.sink.root / "heads").glob("*.json"))
            head = json.loads(path.read_text())
            head["headHash"] = "0" * 64
            path.write_text(json.dumps(head), encoding="utf-8")
            report = export.verify_export(ws.ledger, ws.sink)
            self.assertTrue(any("head manifest" in problem for problem in report["problems"]))

    def test_a_tampered_local_chain_is_refused_for_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 3)
            lines = ws.lines()
            event = json.loads(lines[1])
            event["outcome"] = "deny"
            lines[1] = json.dumps(event)
            ws.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(export.LedgerExportError, "event hash mismatch"):
                ws.export()
            self.assertEqual(ws.sink.records(), [])

    def test_gap_helper_lists_missing_sequences(self):
        self.assertEqual(list(export.iter_gap_sequences([1, 2, 5, 7])), [3, 4, 6])
        self.assertEqual(list(export.iter_gap_sequences([])), [])


class WebhookSinkTests(unittest.TestCase):
    def test_https_is_required(self):
        with self.assertRaises(ValueError):
            export.WebhookSink("http://siem.example/ingest")

    def test_batches_carry_idempotency_keys_and_bearer_token(self):
        sent = []

        def transport(req):
            sent.append(req)
            return 202

        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 3)
            sink = export.WebhookSink("https://siem.example/ingest", token="t0ken", transport=transport)
            export.export_ledger(ws.ledger, sink, ws.state, batch_size=2, exported_at=5)
        self.assertEqual(len(sent), 3)  # two record batches and one head
        first = json.loads(sent[0].data)
        self.assertEqual(first["kind"], "records")
        self.assertEqual(len(first["records"]), 2)
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer t0ken")
        self.assertEqual(len(sent[0].get_header("Idempotency-key")), 64)
        self.assertEqual(json.loads(sent[2].data)["head"]["sequence"], 3)

    def test_failed_delivery_does_not_advance_the_cursor(self):
        def transport(req):
            raise urllib.error.URLError("down")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 2)
            sink = export.WebhookSink("https://siem.example/ingest", transport=transport)
            with self.assertRaisesRegex(export.LedgerExportError, "URLError"):
                export.export_ledger(ws.ledger, sink, ws.state)
            self.assertFalse(ws.state.exists())

    def test_non_success_status_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 1)
            sink = export.WebhookSink("https://siem.example/ingest", transport=lambda req: 500)
            with self.assertRaisesRegex(export.LedgerExportError, "500"):
                export.export_ledger(ws.ledger, sink, ws.state)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_export_then_verify_then_detect_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 4)
            args = ("--ledger", str(ws.ledger))
            code, out, _ = self.run_cli("export", *args, "--state", str(ws.state), "--sink-dir", str(ws.sink.root))
            self.assertEqual((code, json.loads(out)["exported"]), (0, 4))
            code, out, err = self.run_cli("verify", *args, "--sink-dir", str(ws.sink.root))
            self.assertEqual((code, json.loads(out)["ok"], err), (0, True, ""))
            ws.ledger.write_text("\n".join(ws.lines()[:2]) + "\n", encoding="utf-8")
            code, _, err = self.run_cli("verify", *args, "--sink-dir", str(ws.sink.root))
            self.assertEqual(code, 2)
            self.assertIn("ALERT: local ledger truncated", err)
            code, _, err = self.run_cli("export", *args, "--state", str(ws.state), "--sink-dir", str(ws.sink.root))
            self.assertEqual(code, 2)
            self.assertIn("ALERT:", err)

    def test_missing_token_environment_variable_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 1)
            os.environ.pop("LEDGER_EXPORT_TEST_TOKEN", None)
            code, _, err = self.run_cli(
                "export", "--ledger", str(ws.ledger), "--state", str(ws.state),
                "--webhook", "https://siem.example/ingest", "--token-env", "LEDGER_EXPORT_TEST_TOKEN",
            )
            self.assertEqual(code, 1)
            self.assertIn("LEDGER_EXPORT_TEST_TOKEN", err)


if __name__ == "__main__":
    unittest.main()
