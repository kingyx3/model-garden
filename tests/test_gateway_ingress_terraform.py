from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INGRESS = ROOT / "infra" / "docker-host" / "gcp" / "gateway-ingress.tf"


def _variable(source: str, name: str) -> str:
    match = re.search(r'variable "%s" \{\n(.*?)\n\}\n' % re.escape(name), source, re.S)
    assert match, f"variable {name} missing"
    return match.group(1)


class GatewayIngressTerraformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = INGRESS.read_text(encoding="utf-8")

    def test_ingress_is_off_by_default(self) -> None:
        for name in ("gateway_ingress_enabled", "gateway_smtp_enabled"):
            body = _variable(self.source, name)
            self.assertIn("type        = bool", body)
            self.assertIn("default     = false", body)
        self.assertIn("default     = []", _variable(self.source, "gateway_smtp_source_ranges"))
        for name in ("gateway_domain", "gateway_dns_zone"):
            self.assertIn('default     = ""', _variable(self.source, name))

    def test_every_resource_is_gated(self) -> None:
        resources = re.findall(r'^(resource|data) "[^"]+" "[^"]+" \{\n(.*?)\n\}\n', self.source, re.M | re.S)
        self.assertGreater(len(resources), 10)
        for _, body in resources:
            self.assertRegex(body, r"count\s*=\s*local\.(ingress|smtp|dns)\b")

    def test_variables_validate_and_smtp_is_never_open(self) -> None:
        self.assertIn("validation", _variable(self.source, "gateway_domain"))
        self.assertIn("validation", _variable(self.source, "gateway_dns_zone"))
        ranges = _variable(self.source, "gateway_smtp_source_ranges")
        self.assertIn("length(var.gateway_smtp_source_ranges) > 0", ranges)
        self.assertIn("0.0.0.0/0 is not allowed", ranges)
        self.assertNotRegex(self.source, r'"0\.0\.0\.0/0"')

    def test_firewall_only_allows_google_front_ends(self) -> None:
        self.assertIn('"130.211.0.0/22", "35.191.0.0/16"', self.source)
        self.assertIn("source_ranges = local.google_lb_ip", self.source)
        self.assertIn('request_path = "/healthz"', self.source)

    def test_outputs_exist(self) -> None:
        for name in ("gateway_public_ip", "gateway_url"):
            self.assertIn(f'output "{name}"', self.source)


if __name__ == "__main__":
    unittest.main()
