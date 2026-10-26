"""Signed web-chat knowledge intake: draft-only, bounded, tenant-scoped, never self-approved."""
from __future__ import annotations

import json
import pathlib
import shutil
import unittest

import test_channel_gateway as G


class SignedKnowledgeUploadTests(G.GatewayCase):
    def setUp(self):
        super().setUp()
        self.config = {**self.config, "web": {**self.config["web"], "knowledgeUploads": True}}
        self.gateway = G.gateway.Gateway(
            G.compiled(self.root), self.data, self.config, G.ENV, coworker=self.coworker, http=self.http,
            mail_sender=lambda to, subject, body: self.mail.append((to, body)), clock=lambda: G.NOW,
        )
        self.alice = self.sign_in()
        self.request = {"requestId": "1" * 32, "filename": "rules.md",
                        "content": "# Hours\nOpen 9 to 5.\n\n## Escalation\nCall a human for emergencies.\n"}
        self.headers = {"Authorization": "Bearer " + self.alice["session"], "Origin": "https://chat.acme.example",
                        "Content-Type": "application/json"}

    def upload(self, document=None, headers=None):
        payload = self.request if document is None else document
        return self.gateway.handle("POST", "/channels/web/knowledge-uploads",
                                   self.headers if headers is None else headers, json.dumps(payload).encode())

    def test_authenticated_upload_is_private_draft_with_ledger_and_idempotent_retry(self):
        before = self.gateway.desired_state_path.read_bytes()
        response = self.upload()
        self.assertEqual(response.status, 201, response.body)
        report = json.loads(response.body)
        self.assertEqual(report["status"], "draft")
        self.assertEqual(report["alreadyStaged"], False)
        self.assertNotIn("content", str(report))
        tenant_dir = self.data / "knowledge-uploads" / self.gateway.tenant
        metadata = json.loads((tenant_dir / ("u-" + report["uploadId"] + ".json")).read_text())
        self.assertEqual(metadata["by"], "alice@acme.example")
        self.assertEqual(metadata["status"], "draft")
        self.assertEqual((tenant_dir / ("u-" + report["uploadId"] + ".md")).read_text(), self.request["content"])
        self.assertEqual(self.gateway.desired_state_path.read_bytes(), before)
        self.assertEqual(self.upload().status, 200)
        self.assertTrue(json.loads(self.upload().body)["alreadyStaged"])
        ledger = [json.loads(line) for line in (self.data / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(len([e for e in ledger if e["event"] == "knowledge.upload-staged"]), 1)
        self.assertNotIn("Open 9 to 5", (self.data / "audit.jsonl").read_text())
        self.assertNotIn("Call a human", (self.data / "audit.jsonl").read_text())

    def test_unmapped_and_forged_sender_and_cross_origin_are_denied(self):
        bob = self.sign_in("bob@acme.example")
        self.assertEqual(self.upload(headers={**self.headers, "Authorization": "Bearer " + bob["session"]}).status, 403)
        self.assertEqual(self.upload(headers={**self.headers, "Authorization": "Bearer invalid"}).status, 401)
        self.assertEqual(self.upload(headers={**self.headers, "Origin": "https://evil.example"}).status, 403)
        self.assertEqual(self.upload(headers={k: v for k, v in self.headers.items() if k != "Origin"}).status, 403)
        self.assertEqual(self.upload(headers={**self.headers, "Content-Type": "text/plain"}).status, 415)
        self.assertEqual(self.upload(document={**self.request, "sender": "alice@acme.example"}).status, 400)
        self.assertFalse((self.data / "knowledge-uploads" / self.gateway.tenant).exists())

    def test_bad_name_invalid_text_and_over_budget_are_rejected(self):
        for filename in ("../rules.md", "x/../../rules.md", "rules.pdf", ".hidden.md", "rules.md/"):
            with self.subTest(filename=filename):
                self.assertEqual(self.upload(document={**self.request, "filename": filename}).status, 400)
        for content in ("", "\x00", "x" * (64 * 1024 + 1)):
            with self.subTest(size=len(content)):
                self.assertEqual(self.upload(document={**self.request, "content": content}).status, 400)
        self.assertFalse((self.data / "knowledge-uploads" / self.gateway.tenant).exists())

    def test_replay_with_changed_bytes_fails_and_symlinked_metadata_is_not_followed(self):
        self.assertEqual(self.upload().status, 201)
        self.assertEqual(self.upload(document={**self.request, "content": "# New\nDifferent content"}).status, 409)
        report = json.loads(self.upload().body)
        metadata = self.data / "knowledge-uploads" / self.gateway.tenant / ("u-" + report["uploadId"] + ".json")
        metadata.unlink()
        target = self.root / "outside.json"
        target.write_text("keep")
        metadata.symlink_to(target)
        self.assertEqual(self.upload().status, 409)
        self.assertEqual(target.read_text(), "keep")

    def test_symlinked_store_and_failed_ledger_do_not_claim_success(self):
        path = self.data / "knowledge-uploads"
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "outside"
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.upload().status, 409)
        self.assertEqual(list(target.iterdir()), [])
        path.unlink()
        self.gateway.audit = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("audit offline"))
        self.assertEqual(self.upload().status, 503)
        directory = self.data / "knowledge-uploads" / self.gateway.tenant
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [".upload.lock"])

    def test_same_request_from_other_client_cannot_read_or_replace(self):
        first = json.loads(self.upload().body)
        desired = json.loads(self.gateway.desired_state_path.read_text())
        desired["source"]["client"] = "other-client"
        other_desired = self.root / "other-desired.json"
        other_desired.write_text(json.dumps(desired))
        other = G.gateway.Gateway(
            other_desired, self.data, self.config, G.ENV, coworker=self.coworker, http=self.http,
            mail_sender=lambda to, subject, body: self.mail.append((to, body)), clock=lambda: G.NOW,
        )
        self.gateway = other
        self.alice = self.sign_in()
        self.headers["Authorization"] = "Bearer " + self.alice["session"]
        second = json.loads(self.upload().body)
        self.assertNotEqual(first["uploadId"], second["uploadId"])
        self.assertNotEqual(self.gateway.tenant, "default")

    def test_opt_in_disabled_and_page_does_not_offer_upload(self):
        disabled = {**self.config, "web": {k: v for k, v in self.config["web"].items() if k != "knowledgeUploads"}}
        self.gateway = G.gateway.Gateway(
            G.compiled(self.root), self.data, disabled, G.ENV, coworker=self.coworker, http=self.http,
            mail_sender=lambda to, subject, body: self.mail.append((to, body)), clock=lambda: G.NOW,
        )
        self.assertEqual(self.gateway.handle("POST", "/channels/web/knowledge-uploads", self.headers,
                                             json.dumps(self.request).encode()).status, 404)
        self.assertNotIn(b'id="upload"', self.gateway.handle("GET", "/chat", {}, b"").body)


if __name__ == "__main__":
    unittest.main()
