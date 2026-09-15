from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-client-environment.py"
spec = importlib.util.spec_from_file_location("render_client_environment", SCRIPT)
renderer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(renderer)


class RenderClientEnvironmentTests(unittest.TestCase):
    def _write(self, root: pathlib.Path, document: dict) -> pathlib.Path:
        path = root / "environment.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    def test_renders_nonsecret_channel_and_connector_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(pathlib.Path(tmp), {
                "environment": "dev",
                "runtime": {"profile": "receptionist"},
                "voice": {"provider": "livekit", "number_ref": "secret://voice/dev-number"},
                "calendar": {"connector": "google-calendar", "credential_ref": "secret://google/dev-calendar"},
            })
            rendered = renderer.render(path, "dev")
            self.assertEqual(rendered["kind"], "EnvironmentBinding")
            self.assertEqual(rendered["spec"]["voice"]["provider"], "livekit")
            self.assertEqual(rendered["spec"]["calendar"]["connector"], "google-calendar")
            self.assertEqual(rendered["spec"]["calendar"]["credential_ref"], "secret://google/dev-calendar")

    def test_rejects_raw_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(pathlib.Path(tmp), {
                "environment": "prod",
                "runtime": {"profile": "receptionist"},
                "calendar": {"connector": "google-calendar", "api_key": "raw-secret"},
            })
            with self.assertRaisesRegex(ValueError, "raw credential"):
                renderer.render(path, "prod")

    def test_rejects_non_secret_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(pathlib.Path(tmp), {
                "environment": "prod",
                "runtime": {"profile": "receptionist"},
                "voice": {"number_ref": "plain-value"},
            })
            with self.assertRaisesRegex(ValueError, "secret://"):
                renderer.render(path, "prod")

    def test_rejects_environment_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(pathlib.Path(tmp), {
                "environment": "dev",
                "runtime": {"profile": "receptionist"},
            })
            with self.assertRaisesRegex(ValueError, "does not match expected"):
                renderer.render(path, "prod")


if __name__ == "__main__":
    unittest.main()
