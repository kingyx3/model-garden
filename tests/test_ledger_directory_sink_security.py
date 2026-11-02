"""Fail-closed local ledger directory sink regressions (#80).

These are filesystem trust tests, not evidence of a separately administered
retention-locked GCS bucket or live SIEM acceptance.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ledger_export import Workspace, export, write_ledger  # noqa: E402


class DirectorySinkTrustTests(unittest.TestCase):
    def test_retried_record_rejects_symlink_and_hardlink_even_when_bytes_match(self):
        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                ws = Workspace(tmp)
                write_ledger(ws.ledger, 1)
                ws.export()
                record = ws.sink.records()[0]
                target = next((ws.sink.root / "records").glob("*.json"))
                outside = ws.root / "outside.json"
                outside.write_bytes(target.read_bytes())
                target.unlink()
                if attack == "symlink":
                    target.symlink_to(outside)
                else:
                    os.link(outside, target)
                with self.assertRaises(export.LedgerExportError):
                    ws.sink.put_records([record])
                with self.assertRaises(export.LedgerExportError):
                    ws.sink.records()
                self.assertEqual(outside.read_bytes(), target.read_bytes())

    def test_symlinked_record_directory_cannot_redirect_export_outside_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            outside = ws.root / "outside"
            outside.mkdir()
            ws.sink.root.mkdir(mode=0o700)
            (ws.sink.root / "records").symlink_to(outside, target_is_directory=True)
            write_ledger(ws.ledger, 1)
            with self.assertRaises(export.LedgerExportError):
                ws.export()
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse(ws.state.exists())

    def test_symlinked_sink_root_cannot_redirect_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            outside = ws.root / "outside"
            outside.mkdir()
            ws.sink.root.symlink_to(outside, target_is_directory=True)
            write_ledger(ws.ledger, 1)
            with self.assertRaises(export.LedgerExportError):
                ws.export()
            self.assertEqual(list(outside.iterdir()), [])

    def test_short_read_cannot_hide_trailing_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            write_ledger(ws.ledger, 1)
            ws.export()
            head = next((ws.sink.root / "heads").glob("*.json"))
            original = head.read_bytes()
            head.write_bytes(original + b"untrusted trailing bytes")
            real_read = os.read

            # POSIX permits short reads even for regular files. A single read
            # ending after valid JSON must not hide trailing tampering.
            def short_read(fd, count):
                return real_read(fd, min(count, len(original)))

            with mock.patch.object(export.os, "read", side_effect=short_read):
                with self.assertRaises(export.LedgerExportError):
                    ws.sink.heads()

    def test_verifier_refuses_linked_or_public_exported_head(self):
        for attack in ("symlink", "hardlink", "public"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                ws = Workspace(tmp)
                write_ledger(ws.ledger, 1)
                ws.export()
                head = next((ws.sink.root / "heads").glob("*.json"))
                outside = ws.root / "head.json"
                outside.write_bytes(head.read_bytes())
                if attack == "public":
                    head.chmod(0o666)
                else:
                    head.unlink()
                    if attack == "symlink":
                        head.symlink_to(outside)
                    else:
                        os.link(outside, head)
                with self.assertRaises(export.LedgerExportError):
                    export.verify_export(ws.ledger, ws.sink)


if __name__ == "__main__":
    unittest.main()
