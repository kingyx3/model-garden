"""Security regression: private runtime readiness is not an unauthenticated public endpoint (#231)."""
from __future__ import annotations

import http.client
from http.server import ThreadingHTTPServer
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_channel_gateway as base  # noqa: E402

gateway = base.gateway
heartbeat = gateway._module("heartbeat")
T0 = 1_800_000_000


class PrivateReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.instance = gateway.Gateway(base.compiled(self.root), self.root / "data", {}, {}, clock=lambda: T0)

    def probe(self, route, peer, headers=None):
        with mock.patch.object(gateway.time, "time", return_value=T0):
            return self.instance.handle("GET", route, headers or {}, b"", peer_ip=peer)

    def test_remote_clients_cannot_query_gateway_or_worker_even_with_spoofed_proxy_headers(self):
        for route in ("/health/gateway", "/health/worker"):
            for peer in ("203.0.113.9", "2001:db8::4", "::ffff:127.0.0.1", "", "not-an-ip", None):
                with self.subTest(route=route, peer=peer):
                    response = self.probe(route, peer, {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "::1"})
                    self.assertEqual((response.status, response.body), (404, b"not found"))
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_only_actual_loopback_peer_can_read_real_readiness(self):
        for route, component in (("/health/gateway", "gateway"), ("/health/worker", "worker")):
            with self.subTest(route=route):
                self.assertEqual(self.probe(route, "127.0.0.1").status, 503)
                heartbeat.Beat(self.root / "data" / component / "heartbeat.json").write(T0, {"announce-receipts" if component == "gateway" else "scheduler": False})
                self.assertEqual(self.probe(route, "::1").status, 200)
                self.assertEqual(self.probe(route, "203.0.113.9").status, 404)
        self.assertEqual(self.instance.handle("GET", "/healthz", {}, b"", peer_ip="203.0.113.9").status, 200)

    def test_legacy_public_readiness_requires_explicit_validated_configuration(self):
        for bad in (True, False, "public", "private ", "", 1):
            with self.subTest(bad=bad):
                with self.assertRaises(gateway.GatewayConfigError):
                    gateway.load_config({"readinessExposure": bad})
        self.assertEqual(gateway.load_config({})["readinessExposure"], "private")
        self.assertEqual(gateway.load_config({"readinessExposure": "legacy-public"})["readinessExposure"], "legacy-public")
        legacy = gateway.Gateway(base.compiled(self.root), self.root / "data",
                                 {"readinessExposure": "legacy-public"}, {}, clock=lambda: T0)
        with mock.patch.object(gateway.time, "time", return_value=T0):
            self.assertEqual(legacy.handle("GET", "/health/gateway", {}, b"", peer_ip="203.0.113.9").status, 503)

    def test_http_transport_passes_actual_socket_peer_not_untrusted_headers(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.make_handler(self.instance))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            with mock.patch.object(self.instance, "handle", wraps=self.instance.handle) as spy:
                conn.request("GET", "/health/worker", headers={"X-Forwarded-For": "203.0.113.9"})
                response = conn.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                self.assertEqual(spy.call_args.kwargs["peer_ip"], "127.0.0.1")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
