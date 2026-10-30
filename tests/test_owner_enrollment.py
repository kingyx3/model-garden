"""Owner-entered credentials end to end (#159, #86, #129): sealed vault, the gateway's enrollment page, the operator command,
the sidecar's resolver and the deployment wiring. No real provider, mail relay or credential is involved."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vault_module = load("sealed_vault_under_test", "platform/core/sealed_vault.py")
upstream_http = load("upstream_http_for_enrollment", "platform/runtime/upstream_http.py")
gateway_module = load("gateway_for_enrollment", "platform/runtime/gateway.py")
connection_cli = load("runtime_connection_for_enrollment", "scripts/runtime-connection.py")
deployer = load("deployer_for_enrollment", "scripts/deploy-client-docker.py")

KEYS = vault_module.generate_keypair()
OTHER_KEYS = vault_module.generate_keypair()
VALUE = "sk-owner-entered-NEVER-LOGGED"
STABLE = "secret://acme-dev/receptionist/orders-api"
NOW = 1_800_000_000


class SealedVaultTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name) / "vault"
        self.vault = vault_module.SealedVault(self.root, public_key=KEYS["public"])
        self.resolver = vault_module.SealedResolver(self.root, private_key=KEYS["private"])

    def test_a_value_opens_only_with_the_private_key_and_never_sits_on_disk_in_clear(self):
        self.vault.put(STABLE + "/v1", VALUE)
        self.assertEqual(self.resolver(STABLE + "/v1"), VALUE)
        self.assertEqual(self.resolver(STABLE), VALUE)
        on_disk = "".join(path.read_text() for path in self.root.iterdir())
        self.assertNotIn(VALUE, on_disk)
        self.assertNotIn("orders-api", on_disk + "".join(path.name for path in self.root.iterdir()))
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")
        self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in self.root.iterdir()))
        with self.assertRaisesRegex(vault_module.VaultError, "unavailable"):
            vault_module.SealedResolver(self.root, private_key=OTHER_KEYS["private"])(STABLE)

    def test_a_sealed_file_cannot_be_moved_to_another_reference(self):
        self.vault.put("secret://acme-dev/a/conn/v1", VALUE)
        self.vault.put("secret://acme-dev/b/conn/v1", "other")
        a = self.root / vault_module._name("secret://acme-dev/a/conn/v1", ".sealed")
        b = self.root / vault_module._name("secret://acme-dev/b/conn/v1", ".sealed")
        b.write_text(a.read_text())
        with self.assertRaisesRegex(vault_module.VaultError, "unavailable"):
            self.resolver("secret://acme-dev/b/conn/v1")

    def test_rotation_moves_the_stable_reference_and_revocation_never_falls_back(self):
        self.vault.put(STABLE + "/v1", "first")
        self.vault.put(STABLE + "/v2", "second")
        self.assertEqual(self.resolver(STABLE), "second")
        self.vault.delete(STABLE + "/v1")
        self.assertEqual(self.resolver(STABLE), "second")
        self.vault.put(STABLE + "/v1", "stale")  # an older version written late never takes over
        self.assertEqual(self.resolver(STABLE), "second")
        self.vault.delete(STABLE + "/v2")
        self.assertFalse(self.resolver.has(STABLE))
        with self.assertRaisesRegex(vault_module.VaultError, "unavailable"):
            self.resolver(STABLE)

    def test_a_vault_without_a_public_key_only_deletes_and_bad_keys_or_links_are_refused(self):
        self.vault.put(STABLE + "/v1", VALUE)
        delete_only = vault_module.SealedVault(self.root)
        with self.assertRaises(vault_module.VaultError):
            delete_only.put(STABLE + "/v2", VALUE)
        delete_only.delete(STABLE + "/v1")
        self.assertFalse(self.resolver.has(STABLE))
        for bad in ("not-base64!", "c2hvcnQ="):
            with self.assertRaises(vault_module.VaultError):
                vault_module.SealedVault(self.root, public_key=bad)
        with self.assertRaises(vault_module.VaultError):
            self.vault.put("https://example.com", VALUE)
        os.symlink(self.root, self.root.parent / "link")
        with self.assertRaises(vault_module.VaultError):
            vault_module.SealedVault(self.root.parent / "link", public_key=KEYS["public"]).put(STABLE + "/v3", VALUE)

    def test_the_sidecar_resolves_the_vault_first_then_injected_variables(self):
        self.vault.put(STABLE + "/v1", VALUE)
        environ = {"MODEL_GARDEN_VAULT_PRIVATE_KEY": KEYS["private"], "MODEL_GARDEN_VAULT_ROOT": str(self.root),
                   upstream_http.secret_env_name("secret://deploy/time"): "from-env"}
        resolve = upstream_http.secret_resolver(environ)
        self.assertEqual(resolve(STABLE), VALUE)
        self.assertEqual(resolve("secret://deploy/time"), "from-env")
        with self.assertRaises(upstream_http.UpstreamTransportError):
            resolve("secret://nowhere/at/all")
        self.assertIsInstance(upstream_http.secret_resolver({}), upstream_http.EnvSecretResolver)
        wrong = upstream_http.secret_resolver({**environ, "MODEL_GARDEN_VAULT_PRIVATE_KEY": OTHER_KEYS["private"]})
        with self.assertRaises(upstream_http.UpstreamTransportError):
            wrong(STABLE)


def compiled(root: pathlib.Path) -> pathlib.Path:
    path = root / "desired-state.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                    "--agent", "receptionist", "--output", str(path)], check=True, capture_output=True)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["source"]["client"] = "acme"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class EnrollmentPageTests(unittest.TestCase):
    config = {
        "publicOrigin": "https://chat.acme.example",
        "directory": {"webchat": {"owner@acme.example": {"identity": "owner@acme.example", "roles": ["approver"]}}},
        "web": {"allowedAddresses": ["owner@acme.example", "bob@acme.example"], "mail": {"from": "chat@acme.example", "host": "smtp.acme.example"}},
        "enrollment": {"vaultPublicKey": KEYS["public"]},
    }

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.data = self.root / "data"
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"client": "acme", "environment": "dev", "capabilities": []}))
        self.mail = []
        self.gateway = gateway_module.Gateway(
            compiled(self.root), self.data, self.config,
            {"MODEL_GARDEN_WEBCHAT_KEY": "k" * 40, "MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.manifest)},
            mail_sender=lambda to, subject, body: self.mail.append((to, body)), clock=time.time,  # the operator command uses the real clock
        )

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = connection_cli.main([str(self.data), *argv, "--manifest", str(self.manifest)])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)

    def open_link(self):
        code, opened = self.cli("credential", "open", "--agent", "receptionist", "--connection", "orders-api", "--kind", "bearer",
                                "--owner", "owner@acme.example", "--actor", "jordan@acme.example", "--origin", "https://chat.acme.example")
        self.assertEqual(code, 0)
        self.assertEqual(opened["secretRef"], STABLE)
        return opened["link"].split("#enroll=")[1]

    def session_for(self, address, enroll):
        self.gateway.handle("POST", "/channels/web/login", {}, json.dumps({"address": address, "enroll": enroll}).encode())
        link = self.mail[-1][1].split()[-1]
        self.assertTrue(link.startswith("https://chat.acme.example/connections/enroll#login="))
        self.assertTrue(link.endswith("&enroll=" + enroll))
        login = urllib.parse.unquote(link.split("#login=")[1].split("&")[0])
        response = self.gateway.handle("POST", "/channels/web/session", {}, json.dumps({"login": login}).encode())
        return json.loads(response.body)["session"]

    def submit(self, token, value, session=None):
        headers = {"Authorization": f"Bearer {session}"} if session else {}
        response = self.gateway.handle("POST", "/connections/enroll", headers, json.dumps({"token": token, "value": value}).encode())
        return response.status, json.loads(response.body)

    def test_only_the_named_owner_signed_in_can_store_and_the_sidecar_can_then_use_it(self):
        token = self.open_link()
        page = self.gateway.handle("GET", "/connections/enroll", {}, b"")
        self.assertEqual(page.status, 200)
        self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
        self.assertEqual(self.gateway.handle("GET", "/connections/enroll/app.js", {}, b"").status, 200)
        self.assertEqual(self.submit(token, VALUE)[0], 401)
        self.assertEqual(self.submit(token, VALUE, self.session_for("bob@acme.example", token))[0], 400)
        status, body = self.submit(token, VALUE, self.session_for("owner@acme.example", token))
        self.assertEqual((status, body["version"]), (200, 1))
        self.assertNotIn(VALUE, json.dumps(body))
        self.assertEqual(self.submit(token, "again", self.session_for("owner@acme.example", token))[0], 400)  # single use
        resolver = vault_module.SealedResolver(self.data / "vault", private_key=KEYS["private"])
        self.assertEqual(resolver(STABLE), VALUE)
        audit = (self.data / "audit.jsonl").read_text()
        self.assertIn("secret.set", audit)
        self.assertNotIn(VALUE, audit)
        self.assertNotIn(token, audit)
        code, state = self.cli("credential", "status", "--agent", "receptionist", "--connection", "orders-api")
        self.assertEqual((code, state["state"], state["masked"]), (0, "set", f"set ({len(VALUE)} characters)"))
        self.assertNotIn("ref", state)

    def test_rotation_keeps_the_stable_reference_and_revocation_deletes_it(self):
        first = self.open_link()
        self.submit(first, "first-value", self.session_for("owner@acme.example", first))
        second = self.open_link()
        self.assertEqual(self.submit(second, "second-value", self.session_for("owner@acme.example", second))[1]["version"], 2)
        resolver = vault_module.SealedResolver(self.data / "vault", private_key=KEYS["private"])
        self.assertEqual(resolver(STABLE), "second-value")
        self.assertFalse(resolver.has(STABLE + "/v1"))
        code, _ = self.cli("credential", "revoke", "--agent", "receptionist", "--connection", "orders-api", "--actor", "owner@acme.example")
        self.assertEqual(code, 0)
        self.assertFalse(resolver.has(STABLE))

    def test_enrollment_needs_web_sign_in_a_valid_key_and_a_known_environment(self):
        bare = {key: value for key, value in self.config.items() if key != "web"}
        with self.assertRaisesRegex(gateway_module.GatewayConfigError, "web sign-in"):
            gateway_module.load_config(bare)
        with self.assertRaisesRegex(gateway_module.GatewayConfigError, "X25519"):
            gateway_module.load_config({**self.config, "enrollment": {"vaultPublicKey": "nope"}})
        with self.assertRaisesRegex(gateway_module.GatewayConfigError, "capability manifest"):
            gateway_module.Gateway(compiled(self.root), self.data, self.config, {"MODEL_GARDEN_WEBCHAT_KEY": "k" * 40})
        plain = gateway_module.Gateway(compiled(self.root), self.data, {k: v for k, v in self.config.items() if k != "enrollment"},
                                       {"MODEL_GARDEN_WEBCHAT_KEY": "k" * 40}, mail_sender=lambda *a: self.mail.append(a), clock=lambda: NOW)
        self.assertEqual(plain.handle("GET", "/connections/enroll", {}, b"").status, 404)
        plain.handle("POST", "/channels/web/login", {}, json.dumps({"address": "owner@acme.example", "enroll": "x" * 43}).encode())
        self.assertIn("/chat#login=", self.mail[-1][2])


class DeploymentTests(unittest.TestCase):
    def test_private_key_goes_to_the_sidecar_only_and_the_gateway_gets_the_public_key(self):
        helper = sys.modules.get("test_deploy_client_docker") or load("test_deploy_client_docker", "tests/test_deploy_client_docker.py")
        case = helper.DeployClientDockerTests("test_secret_resolution_requires_exact_reference_set")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            profile = case._profile(root)
            gateway = {
                "enabled": True, "public_origin": "https://chat.acme.example", "secrets": {"webchat_key_ref": "secret://webchat/key"},
                "web": {"allowed_addresses": ["owner@acme.example"], "mail": {"from": "chat@acme.example", "host": "smtp.acme.example"}},
                "enrollment": {"vault_public_key": KEYS["public"]},
            }
            upstream = {"mcp": {"enabled": True, "secret_refs": ["secret://hubspot/receptionist"], "vault_key_ref": "secret://vault/private"}}
            binding = case._binding(root, {"gateway": gateway, "upstream": upstream})
            deployer.render_bundle(profile, binding, root / "bundle", "acme-dev")
            compose = yaml.safe_load((root / "bundle" / "compose.yaml").read_text(encoding="utf-8"))
            self.assertIn("MODEL_GARDEN_VAULT_PRIVATE_KEY", compose["services"]["governed-tools"]["environment"])
            for name, service in compose["services"].items():
                if name != "governed-tools":
                    self.assertNotIn("MODEL_GARDEN_VAULT_PRIVATE_KEY", json.dumps(service))
            config = json.loads((root / "bundle" / "profile" / ".modelgarden" / "gateway.json").read_text(encoding="utf-8"))
            self.assertEqual(config["enrollment"], {"vaultPublicKey": KEYS["public"]})
            for module in ("sealed_vault.py", "secret_enrollment.py"):
                self.assertTrue((root / "bundle" / "model-garden-platform" / "platform" / "core" / module).is_file())
            no_web = {key: value for key, value in gateway.items() if key != "web"}
            no_web["slack"] = {"bot_user_id": "U0BOT0001"}
            with self.assertRaisesRegex(ValueError, "enrollment"):
                deployer.render_bundle(profile, case._binding(root, {"gateway": no_web}), root / "bad", "acme-dev")
            with self.assertRaisesRegex(ValueError, "vault_key_ref"):
                deployer.render_bundle(profile, case._binding(root, {"upstream": {"mcp": {**upstream["mcp"], "vault_key_ref": "plain"}}}), root / "bad2", "acme-dev")
            no_vault = {"mcp": {"enabled": True, "secret_refs": ["secret://hubspot/receptionist"]}}
            with self.assertRaisesRegex(ValueError, "vault_key_ref"):
                deployer.render_bundle(profile, case._binding(root, {"gateway": gateway, "upstream": no_vault}), root / "bad3", "acme-dev")
            vault_only = {"mcp": {"enabled": True, "vault_key_ref": "secret://vault/private"}}
            deployer.render_bundle(profile, case._binding(root, {"gateway": gateway, "upstream": vault_only}), root / "vault-only", "acme-dev")
            compose = yaml.safe_load((root / "vault-only" / "compose.yaml").read_text(encoding="utf-8"))
            self.assertEqual(compose["services"]["governed-tools"]["environment"]["MODEL_GARDEN_UPSTREAM_MCP"], "enabled")
            with self.assertRaisesRegex(ValueError, "secret_refs"):
                deployer.render_bundle(profile, case._binding(root, {"upstream": {"mcp": {"enabled": True}}}), root / "bad4", "acme-dev")

    def test_a_deployment_refuses_a_gateway_key_the_sidecar_cannot_open(self):
        config = {"enrollment": {"vaultPublicKey": KEYS["public"]}}
        deployer._check_vault_pair(config, KEYS["private"])
        deployer._check_vault_pair(None, KEYS["private"])
        for private in (OTHER_KEYS["private"], "not-a-key"):
            with self.assertRaisesRegex(ValueError, "does not match"):
                deployer._check_vault_pair(config, private)


if __name__ == "__main__":
    unittest.main()
