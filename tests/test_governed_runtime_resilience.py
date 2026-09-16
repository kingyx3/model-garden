from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_PATH = ROOT / "platform" / "runtime" / "governed_mcp.py"

runtime_spec = importlib.util.spec_from_file_location("governed_runtime_resilience", RUNTIME_PATH)
runtime_module = importlib.util.module_from_spec(runtime_spec)
assert runtime_spec.loader is not None
runtime_spec.loader.exec_module(runtime_module)


class GovernedRuntimeResilienceTests(unittest.TestCase):
    def _runtime(self, root: pathlib.Path):
        desired = root / "desired.json"
        desired.write_text(json.dumps({"schemaVersion": 1}), encoding="utf-8")
        return runtime_module.GovernedToolRuntime(
            desired,
            root / "audit.jsonl",
            root / "approvals",
            environ={},
        )

    def test_installed_hermes_package_is_runtime_version_authority(self) -> None:
        with mock.patch.object(runtime_module.importlib.metadata, "version", return_value="9.8.7"):
            self.assertEqual(runtime_module._hermes_version(), "9.8.7")

    def test_stale_crashed_claim_does_not_block_fresh_approval(self) -> None:
        request_id = "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self._runtime(root)
            approval_root = root / "approvals"
            stale = approval_root / "claimed" / f"{request_id}.stale.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("{}", encoding="utf-8")

            decision_path = approval_root / "decisions" / f"{request_id}.json"
            decision_path.parent.mkdir(parents=True)
            decision_path.write_text(
                json.dumps({"requestId": request_id, "approved": True, "approver": "owner"}),
                encoding="utf-8",
            )

            decision, claimed = runtime._take_approval(request_id)

            self.assertEqual(decision["requestId"], request_id)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertNotEqual(claimed, stale)
            self.assertTrue(claimed.is_file())
            self.assertTrue(stale.is_file())
            self.assertFalse(decision_path.exists())

            runtime._discard_claim(claimed)
            self.assertFalse(claimed.exists())
            self.assertTrue(stale.is_file())


if __name__ == "__main__":
    unittest.main()
