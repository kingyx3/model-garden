"""Regression tests for the private Hermes transport boundary (#235/#117).

These loopback-only fixtures exercise urllib itself, not an injected fake transport.
They do not prove real model/provider behavior or deployed network isolation.
"""
from __future__ import annotations

import http.server
import os
import threading
import unittest
import urllib.request
from unittest import mock

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "coworker_transport_security", ROOT / "platform" / "runtime" / "coworker_client.py"
)
coworker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = coworker
SPEC.loader.exec_module(coworker)

KEY = "synthetic-hermes-only-key-123"


class _Endpoint:
    def __init__(self, status=200, location=None):
        self.received = []
        self.status = status
        self.location = location

    def __enter__(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self):
                size = int(self.headers.get("Content-Length", "0"))
                data = self.rfile.read(size) if size else b""
                owner.received.append((self.command, self.path, self.headers.get("Authorization"), data))
                self.send_response(owner.status)
                if owner.location:
                    self.send_header("Location", owner.location)
                self.end_headers()
                self.wfile.write(b"synthetic")

            def do_POST(self):
                self._reply()

            def do_GET(self):
                self._reply()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def port(self):
        return self.server.server_address[1]

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class PrivateHermesTransportTests(unittest.TestCase):
    def test_hermes_redirect_cannot_forward_bearer_or_customer_prompt(self):
        with _Endpoint() as foreign:
            with _Endpoint(302, f"http://127.0.0.1:{foreign.port}/capture") as hermes:
                status, body = coworker._urllib_transport(
                    f"http://localhost:{hermes.port}/v1/chat/completions",
                    b"synthetic-private-customer-message",
                    {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                    3,
                )
                self.assertEqual(status, 302)
                self.assertEqual(body, b"")
                self.assertEqual(len(hermes.received), 1)
                self.assertEqual(foreign.received, [], "redirect target must receive no request or credentials")

    def test_ambient_http_proxy_cannot_intercept_private_model_requests(self):
        with _Endpoint(status=418) as proxy, _Endpoint(status=200) as hermes:
            with mock.patch.dict(os.environ, {
                "http_proxy": f"http://127.0.0.1:{proxy.port}",
                "HTTP_PROXY": f"http://127.0.0.1:{proxy.port}",
                "no_proxy": "",
                "NO_PROXY": "",
            }):
                # A host's proxy-bypass settings are not a security boundary.
                with mock.patch.object(urllib.request, "proxy_bypass", return_value=False):
                    status, body = coworker._urllib_transport(
                        f"http://localhost:{hermes.port}/v1/chat/completions",
                        b"synthetic-private-customer-message",
                        {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                        3,
                    )
            self.assertEqual(status, 200)
            self.assertEqual(body, b"synthetic")
            self.assertEqual(proxy.received, [], "environment proxy must receive no request or credentials")
            self.assertEqual(len(hermes.received), 1)
            self.assertEqual(hermes.received[0][2], f"Bearer {KEY}")

    def test_normal_private_hermes_request_still_works(self):
        with _Endpoint() as hermes:
            status, body = coworker._urllib_transport(
                f"http://localhost:{hermes.port}/v1/chat/completions",
                b"synthetic",
                {"Authorization": f"Bearer {KEY}"},
                3,
            )
            self.assertEqual((status, body), (200, b"synthetic"))
            self.assertEqual(len(hermes.received), 1)


if __name__ == "__main__":
    unittest.main()
