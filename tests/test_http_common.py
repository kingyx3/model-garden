"""Credential-free checks for the shared constrained HTTPS transport used by the connectors."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("http_common_under_test", ROOT / "platform/connectors/http_common.py")
http_common = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["http_common_under_test"] = http_common
spec.loader.exec_module(http_common)
HOST = re.compile(r"^api\.example\.test$")


class BodyTests(unittest.TestCase):
    def test_json_text_and_multipart_payloads_set_their_own_content_type(self):
        headers: dict[str, str] = {}
        self.assertIsNone(http_common._body(None, headers))
        self.assertEqual(json.loads(http_common._body({"a": 1}, headers)), {"a": 1})
        self.assertEqual(headers["Content-Type"], "application/json")
        text_headers: dict[str, str] = {}
        self.assertEqual(http_common._body({"_text": "hello", "_contentType": "text/csv"}, text_headers), b"hello")
        self.assertEqual(text_headers["Content-Type"], "text/csv")
        multi: dict[str, str] = {}
        body = http_common._body({"_multipart": {"metadata": {"name": "a"}, "content": "body text", "contentType": "text/markdown"}}, multi).decode()
        boundary = multi["Content-Type"].split("boundary=")[1]
        self.assertTrue(body.startswith(f"--{boundary}\r\n") and body.endswith(f"--{boundary}--"))
        self.assertIn('{"name": "a"}', body)
        self.assertIn("body text", body)

    def test_a_non_object_payload_is_refused(self):
        for bad in ("text", ["a"], 5):
            with self.assertRaises(ValueError):
                http_common._body(bad, {})

    def test_responses_are_json_empty_or_raw_text(self):
        self.assertEqual(http_common._parse(""), {})
        self.assertEqual(http_common._parse('{"a": 1}'), {"a": 1})
        self.assertEqual(http_common._parse("plain words"), {"_text": "plain words"})


class TransportTests(unittest.TestCase):
    def test_only_the_fixed_https_host_without_a_port_is_reachable(self):
        transport = http_common.make_transport("Example", HOST)
        for url in ("http://api.example.test/x", "https://other.example/x", "https://api.example.test:8443/x", "https://api.example.test.evil.example/x",
                    "ftp://api.example.test/x", "file:///etc/passwd"):
            with self.assertRaisesRegex(ValueError, "fixed HTTPS Example host"):
                transport("GET", url, {"Authorization": "Bearer x"}, None)

    def test_redirects_are_refused_so_a_bearer_token_cannot_follow_one(self):
        with self.assertRaisesRegex(ValueError, "redirect refused"):
            http_common._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/")


if __name__ == "__main__":
    unittest.main()
