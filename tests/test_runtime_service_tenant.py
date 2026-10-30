"""Fail-closed runtime tenant routing regressions (#77, #115).

Synthetic compiled states only: the HTTP gateway and autonomous worker must
refuse an invalid client before opening any shared Tier 0 state. No live
provider or cross-host isolation claim is made here.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


WORKER = load("runtime_tenant_worker", "platform/runtime/worker.py")
GATEWAY = load("runtime_tenant_gateway", "platform/runtime/gateway.py")


class RuntimeTenantRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.state = self.root / "desired.json"
        self.data = self.root / "data"

    def write(self, source):
        self.state.write_text(json.dumps({
            "source": source,
            "agent": {"metadata": {"name": "receptionist"}},
            "tools": [],
        }), encoding="utf-8")

    def test_invalid_compiled_clients_are_refused_before_any_store_is_created(self):
        for source in (None, {}, {"client": None}, {"client": ""}, {"client": "ACME"},
                       {"client": "a/../other"}, {"client": "acme_dev"},
                       {"client": "a" * 64}, {"client": 42}, {"client": []}):
            with self.subTest(source=source):
                self.write(source)
                with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
                    WORKER.Worker(self.state, self.data, {})
                with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
                    GATEWAY.Gateway(self.state, self.data, {}, {})
                self.assertFalse(self.data.exists(), "invalid identity created a shared tenant store")

    def test_valid_compiled_slug_is_used_without_normalization(self):
        self.write({"client": "acme-dev"})
        worker = WORKER.Worker(self.state, self.data, {})
        gateway = GATEWAY.Gateway(self.state, self.data, {}, {})
        self.assertEqual((worker.tenant, gateway.tenant), ("acme-dev", "acme-dev"))

    def test_both_tenant_helpers_reject_missing_or_normalized_aliases(self):
        for runtime in (WORKER, GATEWAY):
            self.assertEqual(runtime.tenant_of({"source": {"client": "acme"}}), "acme")
            for bad in (None, {}, {"source": {"client": "ACME"}},
                        {"source": {"client": "a/../other"}}):
                with self.subTest(runtime=runtime.__name__, bad=bad):
                    with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
                        runtime.tenant_of(bad)


if __name__ == "__main__":
    unittest.main()
