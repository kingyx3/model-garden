"""Credential-free checks for the generated Slack app manifest and Teams app package (#82, #83)."""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("app_manifests_under_test", ROOT / "platform" / "channels" / "app_manifests.py")
M = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = M
spec.loader.exec_module(M)

ORIGIN = "https://coworker.client.example"
APP_ID = "0f1e2d3c-1111-2222-3333-444455556666"
SECRETISH = re.compile(r"secret|token|password|credential|xox[bap]-", re.IGNORECASE)


class SlackTests(unittest.TestCase):
    def test_points_only_at_the_gateway_endpoints(self):
        manifest = M.slack_manifest(ORIGIN, name="Acme Coworker")
        settings = manifest["settings"]
        self.assertEqual(settings["event_subscriptions"]["request_url"], f"{ORIGIN}/channels/slack/events")
        self.assertEqual(settings["interactivity"]["request_url"], f"{ORIGIN}/channels/slack/interactive")

    def test_least_privilege_scopes_and_events(self):
        manifest = M.slack_manifest(ORIGIN, name="Acme Coworker")
        self.assertEqual(manifest["oauth_config"]["scopes"]["bot"], ["app_mentions:read", "chat:write", "im:history"])
        self.assertEqual(manifest["settings"]["event_subscriptions"]["bot_events"], ["app_mention", "message.im"])
        self.assertNotIn("user", manifest["oauth_config"]["scopes"])
        for off in ("org_deploy_enabled", "socket_mode_enabled", "token_rotation_enabled"):
            self.assertIs(manifest["settings"][off], False)

    def test_file_scopes_only_on_request(self):
        scopes = M.slack_manifest(ORIGIN, name="Acme", files=True)["oauth_config"]["scopes"]["bot"]
        self.assertEqual(scopes, sorted(["app_mentions:read", "chat:write", "im:history", "files:read", "files:write"]))

    def test_holds_no_secret(self):
        text = json.dumps(M.slack_manifest(ORIGIN, name="Acme", files=True))
        self.assertIsNone(SECRETISH.search(text.replace("token_rotation_enabled", "")))

    def test_unsafe_settings_are_refused(self):
        for origin in ("http://coworker.client.example", f"{ORIGIN}/", f"{ORIGIN}/path", "https://localhost", "https://a.b:0",
                       "https://a.b:99999", "https://user@a.b", "coworker.client.example", "", None, "https://exa mple.com"):
            with self.assertRaises(M.ManifestError, msg=str(origin)):
                M.slack_manifest(origin, name="Acme")
        for name in ("", " ", "x" * 31, "<script>", None):
            with self.assertRaises(M.ManifestError, msg=str(name)):
                M.slack_manifest(ORIGIN, name=name)
        with self.assertRaises(M.ManifestError):
            M.slack_manifest(ORIGIN, name="Acme", description="x" * 131)


class TeamsTests(unittest.TestCase):
    def manifest(self, **overrides):
        values = {"app_id": APP_ID, "name": "Acme Coworker"}
        values.update(overrides)
        return M.teams_manifest(ORIGIN, **values)

    def test_bot_scopes_domains_and_no_extra_permissions(self):
        manifest = self.manifest()
        bot = manifest["bots"][0]
        self.assertEqual(bot["botId"], APP_ID)
        self.assertEqual(bot["scopes"], ["personal", "team", "groupChat"])
        self.assertIs(bot["supportsFiles"], False)
        self.assertEqual(manifest["validDomains"], ["coworker.client.example"])
        for risky in ("permissions", "webApplicationInfo", "authorization", "composeExtensions", "staticTabs", "configurableTabs"):
            self.assertNotIn(risky, manifest)

    def test_port_is_part_of_the_valid_domain(self):
        self.assertEqual(M.teams_manifest("https://a.client.example:8443", app_id=APP_ID, name="Acme")["validDomains"], ["a.client.example:8443"])

    def test_app_id_is_a_lowercase_guid(self):
        self.assertEqual(self.manifest(app_id=APP_ID.upper())["id"], APP_ID)
        for bad in ("", "not-a-guid", APP_ID + "0", None):
            with self.assertRaises(M.ManifestError, msg=str(bad)):
                self.manifest(app_id=bad)

    def test_version_and_origin_are_checked(self):
        for bad in ("1", "1.0", "v1.0.0", "1.0.0-beta"):
            with self.assertRaises(M.ManifestError, msg=bad):
                self.manifest(package_version=bad)
        with self.assertRaises(M.ManifestError):
            M.teams_manifest("http://x.example", app_id=APP_ID, name="Acme")

    def test_package_holds_the_manifest_and_valid_icons(self):
        archive = zipfile.ZipFile(io.BytesIO(M.teams_package(self.manifest())))
        self.assertEqual(sorted(archive.namelist()), ["color.png", "manifest.json", "outline.png"])
        self.assertEqual(json.loads(archive.read("manifest.json"))["id"], APP_ID)
        for name, size in (("color.png", 192), ("outline.png", 32)):
            data = archive.read(name)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), (size, size))
        self.assertIsNone(archive.testzip())

    def test_package_is_reproducible(self):
        self.assertEqual(M.teams_package(self.manifest()), M.teams_package(self.manifest()))
        self.assertNotEqual(M.teams_package(self.manifest()), M.teams_package(self.manifest(package_version="1.0.1")))

    def test_package_refuses_a_foreign_document(self):
        for bad in ({}, {"manifestVersion": "1.16"}, "x", None):
            with self.assertRaises(M.ManifestError):
                M.teams_package(bad)


class CommandTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "channel-app-manifest.py"), *args], capture_output=True, text=True, timeout=60)

    def test_slack_prints_json_and_teams_writes_a_zip(self):
        slack = self.run_cli("slack", "--origin", ORIGIN, "--name", "Acme")
        self.assertEqual(slack.returncode, 0, slack.stderr)
        self.assertEqual(json.loads(slack.stdout)["settings"]["interactivity"]["is_enabled"], True)
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "teams.zip"
            teams = self.run_cli("teams", "--origin", ORIGIN, "--name", "Acme", "--app-id", APP_ID, "--output", str(out))
            self.assertEqual(teams.returncode, 0, teams.stderr)
            self.assertIn("manifest.json", zipfile.ZipFile(out).namelist())

    def test_bad_input_is_a_clean_failure(self):
        result = self.run_cli("slack", "--origin", "http://x.example", "--name", "Acme")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
