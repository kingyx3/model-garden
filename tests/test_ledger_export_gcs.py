"""Credential-free checks for the GCS ledger-export sink, against a fake storage API (#80)."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ledger_export import cli, export, write_ledger  # noqa: E402 - shared fixtures

SA = "mg-exp-1234abcd@acme-proj.iam.gserviceaccount.com"


class FakeStorage:
    """Just enough of the JSON API: create-only upload, list with paging, media read, and a token mint."""

    def __init__(self, page_size: int = 2) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.page_size = page_size
        self.deny: set[str] = set()

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split("?")[0]))
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "generateAccessToken" in url:
            assert headers["Authorization"] == "Bearer base-token"
            return 200, json.dumps({"accessToken": "export-token"}).encode()
        if "metadata.google.internal" in url:
            assert headers["Metadata-Flavor"] == "Google"
            return 200, json.dumps({"access_token": "base-token"}).encode()
        if method in self.deny:
            return 403, b""
        if method == "POST":
            name = query["name"][0]
            assert query["ifGenerationMatch"] == ["0"]
            if name in self.objects:
                return 412, b""
            self.objects[name] = body
            return 200, b"{}"
        if "alt=media" in url:
            return 200, self.objects[urllib.parse.unquote(parsed.path.rsplit("/o/", 1)[1])]
        prefix = query["prefix"][0]
        names = sorted(name for name in self.objects if name.startswith(prefix))
        start = int(query.get("pageToken", ["0"])[0])
        page = names[start:start + self.page_size]
        out: dict = {"items": [{"name": name} for name in page]}
        if start + self.page_size < len(names):
            out["nextPageToken"] = str(start + self.page_size)
        return 200, json.dumps(out).encode()


class GcsSinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.ledger = self.root / "audit.jsonl"
        write_ledger(self.ledger, 5)
        self.api = FakeStorage()

    def sink(self, **kwargs):
        return export.GcsSink("acme-ledger-export", lambda: "export-token", http=self.api, **kwargs)

    def test_export_then_verify_round_trips_through_the_bucket(self):
        summary = export.export_ledger(self.ledger, self.sink(), self.root / "state.json")
        self.assertGreaterEqual(summary["exported"], 5)
        report = export.verify_export(self.ledger, self.sink())
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["exportedRecords"], 5)

    def test_writes_are_create_only_and_a_repeat_is_harmless(self):
        sink = self.sink()
        export.export_ledger(self.ledger, sink, self.root / "state.json")
        stored = dict(self.api.objects)
        (self.root / "state.json").unlink()  # a lost cursor re-delivers everything
        export.export_ledger(self.ledger, sink, self.root / "state.json")
        self.assertEqual(self.api.objects, stored)

    def test_a_refused_write_raises_so_the_cursor_does_not_advance(self):
        self.api.deny.add("POST")
        with self.assertRaises(export.LedgerExportError):
            export.export_ledger(self.ledger, self.sink(), self.root / "state.json")
        self.assertFalse((self.root / "state.json").exists() and json.loads((self.root / "state.json").read_text()).get("sequence"))

    def test_tampering_with_a_stored_object_is_caught_by_verify(self):
        export.export_ledger(self.ledger, self.sink(), self.root / "state.json")
        name = next(name for name in self.api.objects if name.startswith("records/"))
        record = json.loads(self.api.objects[name])
        record["event"] = {**record.get("event", {}), "tool": "forged"}
        self.api.objects[name] = json.dumps(record).encode()
        self.assertFalse(export.verify_export(self.ledger, self.sink())["ok"])

    def test_a_prefix_keeps_clients_apart_and_bad_names_are_refused(self):
        export.export_ledger(self.ledger, self.sink(prefix="acme/prod"), self.root / "state.json")
        self.assertTrue(all(name.startswith("acme/prod/") for name in self.api.objects))
        for bucket, prefix in (("UPPER", ""), ("ok-bucket", "../x"), ("a", "")):
            with self.assertRaises(ValueError):
                export.GcsSink(bucket, lambda: "t", prefix=prefix, http=self.api)

    def test_the_export_identity_is_minted_by_impersonation_and_must_be_a_service_account(self):
        base = export.metadata_token(self.api)
        token = export.impersonated_token(SA, base, scope="https://www.googleapis.com/auth/devstorage.read_write", http=self.api)
        self.assertEqual(token(), "export-token")
        with self.assertRaises(ValueError):
            export.impersonated_token("someone@example.com", base, scope="x", http=self.api)

    def test_the_http_helper_only_talks_to_google_endpoints(self):
        with self.assertRaises(export.LedgerExportError):
            export._http("GET", "https://evil.example/x", {}, None)

    def test_the_command_builds_a_bucket_sink(self):
        parser_args = cli.argparse.Namespace(gcs_bucket="acme-ledger-export", gcs_impersonate=SA, gcs_prefix="p")
        sink = cli.build_gcs_sink(parser_args)
        self.assertEqual((sink.bucket, sink.prefix), ("acme-ledger-export", "p"))


if __name__ == "__main__":
    unittest.main()
