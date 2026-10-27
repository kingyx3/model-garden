"""Credential-free signed owner review of staged Tier 0 rules; never an automatic promotion."""
import json
import pathlib
import unittest

import test_channel_gateway as G


class OwnerReviewTests(G.GatewayCase):
    def setUp(self):
        base = G.GatewayCase.config
        self.config = {
            **base,
            "directory": {
                **base["directory"],
                "webchat": {
                    **base["directory"]["webchat"],
                    "owner@acme.example": {"identity": "owner@acme.example", "roles": ["approver"]},
                },
            },
            "web": {
                **base["web"],
                "allowedAddresses": ["alice@acme.example", "bob@acme.example", "owner@acme.example"],
                "knowledgeUploads": True,
                "knowledgeReviewOwner": "owner@acme.example",
            },
        }
        super().setUp()
        self.alice = self.sign_in("alice@acme.example")
        self.owner = self.sign_in("owner@acme.example")
        self.headers = {
            "Authorization": "Bearer " + self.owner["session"],
            "Origin": "https://chat.acme.example",
            "Content-Type": "application/json",
        }
        self.document = {
            "requestId": "a" * 32, "filename": "rules.md",
            "content": "# Hours\nOpen from 9 to 5.\n\n## Escalation\nCall a person.\n",
        }
        staged = self.gateway.handle("POST", "/channels/web/knowledge-uploads",
                                     {**self.headers, "Authorization": "Bearer " + self.alice["session"]},
                                     json.dumps(self.document).encode())
        self.assertEqual(staged.status, 201, staged.body)
        self.upload = json.loads(staged.body)
        self.review = {"uploadId": self.upload["uploadId"], "sha256": self.upload["sha256"],
                       "decision": "approve"}

    def submit(self, document=None, headers=None):
        return self.gateway.handle("POST", "/channels/web/knowledge-reviews",
                                   self.headers if headers is None else headers,
                                   json.dumps(self.review if document is None else document).encode())

    def test_owner_review_is_digest_bound_first_wins_and_never_promotes(self):
        before = self.gateway.desired_state_path.read_bytes()
        response = self.submit()
        self.assertEqual(response.status, 201, response.body)
        self.assertEqual(json.loads(response.body)["status"], "reviewed-for-import")
        self.assertEqual(self.submit().status, 200)
        self.assertEqual(self.submit({**self.review, "decision": "reject"}).status, 409)
        self.assertEqual(self.gateway.desired_state_path.read_bytes(), before)
        directory = self.data / "knowledge-uploads" / self.gateway.tenant
        self.assertEqual(json.loads((directory / ("u-" + self.upload["uploadId"] + ".json")).read_text())["status"], "draft")
        receipt = json.loads((directory / ("r-" + self.upload["uploadId"] + ".json")).read_text())
        self.assertEqual(receipt["decision"], "approve")
        self.assertNotIn("owner@acme.example", str(receipt))
        ledger = (self.data / "audit.jsonl").read_text()
        self.assertEqual(ledger.count("knowledge.upload-owner-reviewed"), 1)
        self.assertNotIn("Open from 9 to 5", ledger)

    def test_signed_owner_and_exact_request_required_not_uploader_or_approver_claim(self):
        self.assertEqual(self.submit(headers={**self.headers, "Authorization": "Bearer " + self.alice["session"]}).status, 403)
        bob = self.sign_in("bob@acme.example")
        self.assertEqual(self.submit(headers={**self.headers, "Authorization": "Bearer " + bob["session"]}).status, 403)
        self.assertEqual(self.submit(headers={**self.headers, "Authorization": "Bearer invalid"}).status, 401)
        self.assertEqual(self.submit(headers={**self.headers, "Origin": "https://evil.example"}).status, 403)
        self.assertEqual(self.submit({**self.review, "owner": "owner@acme.example"}).status, 400)
        self.assertEqual(self.submit({**self.review, "tenant": "other-client"}).status, 400)
        self.assertEqual(self.submit({**self.review, "sha256": "0" * 64}).status, 409)
        self.assertFalse((self.data / "knowledge-uploads" / self.gateway.tenant /
                          ("r-" + self.upload["uploadId"] + ".json")).exists())

    def test_tamper_expiry_and_symlink_review_are_fail_closed(self):
        directory = self.data / "knowledge-uploads" / self.gateway.tenant
        content = directory / ("u-" + self.upload["uploadId"] + ".md")
        content.write_text("# Hours\nChanged without approval.\n")
        self.assertEqual(self.submit().status, 409)
        content.write_text(self.document["content"])
        self.gateway.clock = lambda: G.NOW + 366 * 86400
        self.assertEqual(self.submit().status, 409)
        self.gateway.clock = lambda: G.NOW
        outside = self.root / "outside.json"
        outside.write_text("safe")
        (directory / ("r-" + self.upload["uploadId"] + ".json")).symlink_to(outside)
        self.assertEqual(self.submit().status, 409)
        self.assertEqual(outside.read_text(), "safe")

    def test_audit_failure_leaves_unaccepted_review_and_blocks_replay(self):
        directory = self.data / "knowledge-uploads" / self.gateway.tenant
        self.gateway.audit = lambda *args: (_ for _ in ()).throw(OSError("audit unavailable"))
        self.assertEqual(self.submit().status, 503)
        self.assertFalse((directory / ("r-" + self.upload["uploadId"] + ".json")).exists())
        self.assertTrue((directory / (".r-" + self.upload["uploadId"] + ".pending")).exists())
        self.assertEqual(self.submit().status, 409)

    def test_rejected_review_is_immutable_and_removed_with_expired_draft(self):
        self.assertEqual(self.submit({**self.review, "decision": "reject"}).status, 201)
        self.assertEqual(self.submit().status, 409)
        uploads = G.gateway._module("uploads")
        directory = self.data / "knowledge-uploads" / self.gateway.tenant
        removed = uploads.purge_expired(
            self.data / "knowledge-uploads", tenant=self.gateway.tenant,
            now=G.NOW + 366 * 86400, retention_days=365, audit=self.gateway.audit,
            actor="operator:alice", dry_run=False,
        )
        self.assertEqual(removed, [self.upload["uploadId"]])
        self.assertFalse((directory / ("r-" + self.upload["uploadId"] + ".json")).exists())

    def test_review_requires_explicit_owner_configuration(self):
        config = {**self.config, "web": {k: v for k, v in self.config["web"].items()
                                           if k != "knowledgeReviewOwner"}}
        other = G.gateway.Gateway(G.compiled(self.root), self.root / "other-data",
                                  config, G.ENV, coworker=self.coworker, http=self.http,
                                  mail_sender=lambda *args: None, clock=lambda: G.NOW)
        self.assertEqual(other.handle("POST", "/channels/web/knowledge-reviews",
                                      self.headers, json.dumps(self.review).encode()).status, 404)
        with self.assertRaises(G.gateway.GatewayConfigError):
            G.gateway.load_config({**self.config, "web": {**self.config["web"],
                                                           "knowledgeReviewOwner": "stranger@elsewhere.example"}})


if __name__ == "__main__":
    unittest.main()
