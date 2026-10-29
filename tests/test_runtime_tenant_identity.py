"""Regression: Tier 0 operator commands must never silently use a shared tenant."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = (
    "runtime-artifacts.py",
    "runtime-memory.py",
    "runtime-unanswered.py",
    "runtime-notification-prefs.py",
    "runtime-crm-sync.py",
    "runtime-conversations.py",
    "runtime-subject-access.py",
)


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location("tenant_test_" + name.replace("-", "_").replace(".", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TenantIdentityTests(unittest.TestCase):
    def test_every_operator_command_requires_an_exact_compiled_client(self):
        with tempfile.TemporaryDirectory() as temp:
            desired = pathlib.Path(temp) / "desired.json"
            for name in SCRIPTS:
                with self.subTest(script=name):
                    script = load_script(name)
                    desired.write_text(json.dumps({"source": {"client": "acme-prod"}}), encoding="utf-8")
                    self.assertEqual(script.default_tenant(desired), "acme-prod")
                    for bad in (
                        None, "{bad", "[]", "{}", '{"source": {}}',
                        '{"source": {"client": null}}', '{"source": {"client": "ACME"}}',
                        '{"source": {"client": "../other"}}',
                        '{"source": {"client": "a" * 70}}',
                    ):
                        if bad is None:
                            desired.unlink(missing_ok=True)
                        elif '"a" * 70' in bad:
                            desired.write_text(json.dumps({"source": {"client": "a" * 70}}), encoding="utf-8")
                        else:
                            desired.write_text(bad, encoding="utf-8")
                        with self.subTest(script=name, invalid=bad):
                            with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
                                script.default_tenant(desired)

    def test_cli_refuses_unbound_identity_before_creating_a_store(self):
        commands = {
            "runtime-artifacts.py": ["list"],
            "runtime-memory.py": ["list", "--scope", "client"],
            "runtime-unanswered.py": ["list"],
            "runtime-notification-prefs.py": ["counts"],
            "runtime-crm-sync.py": ["list"],
            "runtime-conversations.py": ["stats"],
            "runtime-subject-access.py": ["list-corrections"],
        }
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            env = {**os.environ, "MODEL_GARDEN_DESIRED_STATE": str(base / "missing.json")}
            for name, command in commands.items():
                with self.subTest(script=name):
                    store = base / name.removesuffix(".py")
                    run = subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / name), str(store), *command],
                        capture_output=True, text=True, env=env, check=False,
                    )
                    self.assertNotEqual(run.returncode, 0, run.stdout)
                    self.assertIn("tenant identity unavailable", run.stderr)
                    self.assertFalse(store.exists(), f"{name} created state with an unknown tenant")

    def test_explicit_operator_tenant_and_valid_compiled_tenant_still_work(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            desired = base / "desired.json"
            desired.write_text(json.dumps({"source": {"client": "acme-prod"}}), encoding="utf-8")
            script = str(ROOT / "scripts" / "runtime-subject-access.py")
            for options in (["--tenant", "acme-prod"], ["--desired-state", str(desired)]):
                with self.subTest(options=options):
                    run = subprocess.run(
                        [sys.executable, script, str(base / "governance"), *options, "list-corrections"],
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(json.loads(run.stdout), {"corrections": []})


if __name__ == "__main__":
    unittest.main()
