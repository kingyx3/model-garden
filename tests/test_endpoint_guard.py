"""Credential-free checks that a customer-supplied endpoint cannot reach private, metadata or rebinding targets (#159, #87)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("endpoint_guard_under_test", ROOT / "platform/core/endpoint_guard.py")
guard = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["endpoint_guard_under_test"] = guard
spec.loader.exec_module(guard)
Error = guard.EndpointError


def resolver(table):
    return lambda host: table.get(host, [])


class SyntaxTests(unittest.TestCase):
    def test_a_plain_public_https_endpoint_is_accepted(self):
        parts = guard.parse_endpoint("https://mcp.vendor.example/v1/mcp")
        self.assertEqual((parts["scheme"], parts["host"], parts["port"], parts["path"], parts["ipLiteral"]), ("https", "mcp.vendor.example", 443, "/v1/mcp", False))
        self.assertEqual(guard.parse_endpoint("HTTPS://API.Vendor.Example./x")["host"], "api.vendor.example")  # case and trailing dot normalised
        self.assertEqual(guard.parse_endpoint("https://api.vendor.example:8443/x", allow_ports=(443, 8443))["port"], 8443)
        self.assertEqual(guard.parse_endpoint("https://xn--bcher-kva.example/")["host"], "xn--bcher-kva.example")
        self.assertEqual(guard.parse_endpoint("https://93.184.216.34/mcp")["ipLiteral"], True)

    def test_the_scheme_userinfo_fragment_query_and_port_are_restricted(self):
        for url in ("http://api.vendor.example/", "ftp://api.vendor.example/", "file:///etc/passwd", "gopher://api.vendor.example/", "//api.vendor.example/",
                    "api.vendor.example", "https://user:pw@api.vendor.example/", "https://user@api.vendor.example/", "https://api.vendor.example@evil.example/",
                    "https://api.vendor.example/#frag", "https://api.vendor.example/?x=1", "https://api.vendor.example:80/", "https://api.vendor.example:8443/",
                    "https://api.vendor.example:0/", "https://api.vendor.example:99999/", "https://api.vendor.example:abc/", "", None, 5, "x" * 3000):
            with self.assertRaises(Error, msg=str(url)[:60]):
                guard.parse_endpoint(url)  # type: ignore[arg-type]
        self.assertEqual(guard.parse_endpoint("https://api.vendor.example/?v=1", allow_query=True)["query"], "v=1")

    def test_whitespace_control_backslash_and_encoding_tricks_are_refused(self):
        for url in ("https://api.vendor.example\\@evil.example/", "https://api.vendor.example /x", "https://api.vendor.example/\n", "https://api.vendor.example/\x00",
                    "https://api.vendor.example\t/", "https://api%2eevil.example/", "https://api.vendor.example%00.evil.example/", "https://api.vendor.example/a/../b",
                    "https://api.vendor.example/a/%2e%2e/b", "https://api.vendor.example/a%2fb", "https://api.vendor.example/..",
                    "https://bücher.example/", "https://аpi.vendor.example/", "https://api.vendor.example․evil.example/"):
            with self.assertRaises(Error, msg=repr(url)):
                guard.parse_endpoint(url)

    def test_local_internal_and_single_label_names_are_refused(self):
        for host in ("localhost", "LOCALHOST", "localhost.", "api.localhost", "printer.local", "db.internal", "nas.lan", "router.home", "intranet", "wiki.corp",
                     "x.intranet", "y.private", "host.localdomain", "gateway.home.arpa", "metadata", "a", "-bad.example", "bad-.example", "a..b.example", "a_b.example"):
            with self.assertRaises(Error, msg=host):
                guard.parse_endpoint(f"https://{host}/")


class AddressTests(unittest.TestCase):
    def test_every_non_public_ipv4_literal_is_refused(self):
        for address in ("127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254", "169.254.0.1",
                        "100.64.0.1", "100.127.255.254", "0.0.0.0", "255.255.255.255", "224.0.0.1", "240.0.0.1", "192.0.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1", "198.18.0.1"):
            with self.assertRaises(Error, msg=address):
                guard.parse_endpoint(f"https://{address}/")
            self.assertFalse(guard.is_public_ip(address), address)

    def test_alternative_ipv4_spellings_are_refused_not_normalised(self):
        for host in ("2130706433", "0x7f000001", "0x7f.0.0.1", "0177.0.0.1", "017700000001", "127.1", "127.0.1", "1.1.1", "0300.0250.0.1", "4294967295", "0x0a000001",
                     "10.1", "169.254.43518", "00000000127.0.0.1", "127.0.0.1.", "127.0.0.01"):
            with self.assertRaises(Error, msg=host):
                guard.parse_endpoint(f"https://{host}/")

    def test_ipv6_literals_and_embedded_ipv4_forms_are_judged_by_address(self):
        for host in ("[::1]", "[::]", "[fe80::1]", "[fc00::1]", "[fd12:3456::1]", "[fec0::1]", "[ff02::1]", "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[::ffff:169.254.169.254]",
                     "[::ffff:10.0.0.1]", "[2002:7f00:1::]", "[2002:a9fe:a9fe::]", "[64:ff9b::7f00:1]", "[64:ff9b::a00:1]", "[2001:0:4136:e378:8000:63bf:3fff:fdd2]", "[2001:db8::1]",
                     "[::1%25eth0]", "[::1", "::1"):
            with self.assertRaises(Error, msg=host):
                guard.parse_endpoint(f"https://{host}/")
        self.assertEqual(guard.parse_endpoint("https://[2606:4700:4700::1111]/")["ipLiteral"], True)
        self.assertTrue(guard.is_public_ip("2606:4700:4700::1111"))
        self.assertTrue(guard.is_public_ip("::ffff:93.184.216.34"))
        self.assertFalse(guard.is_public_ip("not an address"))

    def test_public_addresses_are_accepted(self):
        for address in ("1.1.1.1", "8.8.8.8", "93.184.216.34", "100.63.255.255", "172.32.0.1", "11.0.0.1"):
            self.assertTrue(guard.is_public_ip(address), address)


class ResolutionTests(unittest.TestCase):
    def test_a_name_that_resolves_to_public_addresses_is_qualified_and_pinned(self):
        result = guard.qualify("https://mcp.vendor.example/x", resolver({"mcp.vendor.example": ["93.184.216.34", "2606:4700:4700::1111", "93.184.216.34"]}))
        self.assertEqual(result["pinned"], ["2606:4700:4700::1111", "93.184.216.34"])
        self.assertEqual(guard.qualify("https://93.184.216.34/x", resolver({}))["pinned"], ["93.184.216.34"])

    def test_one_private_answer_among_public_ones_refuses_the_lot(self):
        for answers in (["127.0.0.1"], ["93.184.216.34", "10.0.0.5"], ["169.254.169.254"], ["::1"], ["::ffff:127.0.0.1"], ["93.184.216.34", "fd00::1"], [], ["not-an-ip"]):
            with self.assertRaises(Error, msg=str(answers)):
                guard.qualify("https://mcp.vendor.example/x", resolver({"mcp.vendor.example": answers}))
        with self.assertRaises(Error):
            guard.qualify("https://mcp.vendor.example/x", resolver({"mcp.vendor.example": [f"93.184.216.{n}" for n in range(1, 40)]}))

    def test_a_resolver_that_fails_refuses_rather_than_trusting_the_name(self):
        def broken(_host):
            raise OSError("no dns")

        with self.assertRaises(Error):
            guard.qualify("https://mcp.vendor.example/x", broken)

    def test_a_connection_to_an_address_that_was_not_pinned_is_refused_even_if_public(self):
        pinned = ["93.184.216.34"]
        self.assertEqual(guard.assert_pinned("93.184.216.34", pinned), "93.184.216.34")
        for address in ("93.184.216.35", "127.0.0.1", "169.254.169.254", "not-an-ip", "::ffff:127.0.0.1"):
            with self.assertRaises(Error, msg=address):
                guard.assert_pinned(address, pinned)
        with self.assertRaises(Error):  # even a pinned entry is refused if it is not public (a poisoned pin set)
            guard.assert_pinned("10.0.0.1", ["10.0.0.1"])

    def test_the_rebinding_sequence_qualify_then_flip_is_caught_at_connect_time(self):
        table = {"mcp.vendor.example": ["93.184.216.34"]}
        pinned = guard.qualify("https://mcp.vendor.example/x", resolver(table))["pinned"]
        table["mcp.vendor.example"] = ["169.254.169.254"]  # the attacker's DNS now answers differently
        with self.assertRaises(Error):
            guard.assert_pinned(table["mcp.vendor.example"][0], pinned)
        with self.assertRaises(Error):
            guard.check_resolution("mcp.vendor.example", resolver(table))


class EgressAndRedirectTests(unittest.TestCase):
    def test_the_egress_allow_list_limits_hosts(self):
        egress = ("mcp.vendor.example", "*.api.partner.example")
        guard.parse_endpoint("https://mcp.vendor.example/", egress=egress)
        guard.parse_endpoint("https://eu.api.partner.example/", egress=egress)
        for url in ("https://other.vendor.example/", "https://api.partner.example/", "https://a.b.api.partner.example/", "https://evil-mcp.vendor.example/", "https://mcp.vendor.example.evil.example/"):
            with self.assertRaises(Error, msg=url):
                guard.parse_endpoint(url, egress=egress)
        with self.assertRaises(Error):
            guard.parse_endpoint("https://mcp.vendor.example/", egress=())  # an empty list allows nothing

    def test_only_same_origin_redirects_are_allowed(self):
        current = "https://mcp.vendor.example/v1/mcp"
        for location in ("/v2/mcp", "v2/mcp", "https://mcp.vendor.example/other", "https://mcp.vendor.example:443/other"):
            self.assertTrue(guard.redirect_allowed(current, location), location)
        for location in ("http://mcp.vendor.example/v1", "https://evil.example/", "//evil.example/x", "https://mcp.vendor.example.evil.example/", "https://mcp.vendor.example:8443/",
                         "https://user@mcp.vendor.example/", "https://127.0.0.1/", "https://mcp.vendor.example/\n", "javascript:alert(1)", "https://mcp.vendor.example\\@evil.example/"):
            self.assertFalse(guard.redirect_allowed(current, location), location)


if __name__ == "__main__":
    unittest.main()
