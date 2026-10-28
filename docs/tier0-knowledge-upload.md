# Signed Tier 0 business-rules intake (draft-only)

This is a bounded **authenticated upload intake**, not an approved KnowledgeSource or live customer acceptance. It is opt-in and requires the existing web-chat magic-link sign-in (and its configured SMTP sign-in delivery). A mapped client member can stage a single UTF-8 `.txt` or `.md` of business rules; unmapped sign-ins cannot upload. Nothing in the request body can impersonate a person, select a tenant or approve content.

Enable `gateway.web.knowledge_uploads: true` in the private EnvironmentBinding and redeploy through the normal reviewed deployment process. The Docker renderer passes the non-secret `knowledgeUploads` flag to the gateway. The `/chat` page then shows a **Stage draft** file input; the same-origin JSON endpoint is `POST /channels/web/knowledge-uploads`, requiring `Authorization: Bearer <signed session>`, `Origin: <configured HTTPS publicOrigin>` and `Content-Type: application/json`. The payload is exactly `{"requestId":"<32 lowercase hex>","filename":"rules.md","content":"# Hours\\n..."}`. Do not send authorization tokens or business rules in URLs. Requests are capped at 64 KiB of UTF-8 file content, with at most 20 pending drafts per client data volume; upload is off by default.

A successful first upload returns HTTP 201 with an opaque `uploadId`, SHA-256, section count and `status: draft`. Repeating the same signed uploader/requestId/content is idempotent (HTTP 200); a changed replay is rejected. Files and metadata are written with exclusive, private permissions under the gateway's per-environment data volume at `knowledge-uploads/<client>/u-<uploadId>.md|txt` and `u-<uploadId>.json`. The upload does not edit the workspace, register a KnowledgeSource, enable a Skill, change a policy or expose uploaded text in the Action Ledger. The ledger receives one `knowledge.upload-staged` receipt (identity, opaque ID, digest, byte count and draft status only). Storage or audit failure cannot return a success response. A crash before the audit receipt leaves a `pending-audit` marker that cannot be replayed as a successful draft; an operator must reconcile and remove or repair it after checking the ledger.

**Separate owner approval and promotion are mandatory.** An authorized operator retrieves the private staged file, verifies the uploader and SHA-256 against the receipt, checks the business rules for prompt injection, correctness, classification, audience and retention, and asks the actual client owner to approve the exact source/version through the existing protected client-workspace change process. The operator can then use `scripts/import-knowledge.py` on a private copy with an approved name, Agent and classification. That command creates a **draft** KnowledgeSource; the owner must separately approve its private workspace diff and promote through reviewed DEV checks before `kb.search` can use it. The uploader's email or the `by` field is **not** owner signoff. No HTTP endpoint changes `spec.status` to approved.


## Optional signed owner digest review (not source promotion)

An operator may configure `gateway.web.knowledge_review_owner: owner@client.example` in the **reviewed private EnvironmentBinding** alongside `knowledge_uploads: true`. The address must also appear in `web.allowed_addresses` and the web-chat principal directory must map it as a client member with the `approver` role. This is disabled if no review owner is configured. The operator must independently verify that the configured mailbox is actually controlled by the authorized client owner; a workspace field is not proof of ownership.

The uploader must be a **different** verified person from the reviewing owner. The owner reviews the private staged content through an authorized operator-controlled channel and verifies the exact SHA-256 before deciding. A signed, tenant-scoped web-chat session for that exact owner can then POST to `/channels/web/knowledge-reviews` with `Authorization: Bearer <session>`, the configured `Origin`, JSON content type and exactly `{"uploadId":"<32 hex>","sha256":"<64 hex>","decision":"approve"}` (or `reject`). No sender, tenant, content or policy fields are accepted in the body. The endpoint is not exposed on the public chat page.

The gateway verifies mapped approver identity and configured owner, tenant, retention, original content digest and receipt. The first decision wins, exact replays are idempotent, conflicting decisions fail closed. A redacted `knowledge.upload-owner-reviewed` Action Ledger event precedes a private `r-<uploadId>.json` review marker. An interrupted audit leaves a non-replayable `.r-<uploadId>.pending` marker requiring operator reconciliation. Retention/explicit erasure removes completed review markers with their source; interrupted reviews block erasure pending investigation.

**An approved-for-import review is NOT an approved KnowledgeSource.** It does not change the draft, import it, authorize a Skill, change policy or deploy a runtime. An authorized operator must still import and validate the exact source in the private workspace; the client owner must separately authorize the protected workspace diff, DEV evaluation and promotion. Signed mailbox access plus a configured owner is not independent client legal signoff or live delivery proof. The actual client-approved deployment and owner key/identity custody remain outstanding (#77, #114, #116).

**Operational limits:** Drafts have bounded expiry and an operator-only audited deletion command, but no automated scheduled cleanup, backup erasure, client-tenant deployment, live SMTP delivery, signed owner approval, push notice, or independently retained immutable export is proved here. Operators must run the retention reconciliation, review interrupted receipts and separately handle backups and imported workspace sources. A full zero-stack acceptance still requires the inbound support address, artifact retention, delivered human notifications and a real client-evaluated deployment (#77). Never describe this credential-free test path as live client proof.


## Artifact retention alignment (v0.8.157)

The reviewed `EnvironmentBinding.spec.worker.artifact_retention_days` (1–3650) now propagates to the governed `docs.create`/`files.*` store and the channel gateway as well as the worker's expiry job. All three use the same tenant retention value; invalid values fail bundle validation. Reducing retention refuses reads of existing artifacts after the new shorter period and the next worker retention pass removes them; increasing retention never resurrects or extends an artifact's original expiry. The default remains 365 days when no policy is specified. This does not delete backup copies or the separately staged knowledge-upload drafts: those still require the client's own retention and erasure process.


## Draft retention and operator reconciliation (v0.8.158)

A signed upload now records `createdAt` and `expiresAt` using the same reviewed
`worker.artifact_retention_days` (default 365, allowed 1–3650) passed to the
gateway as `MODEL_GARDEN_ARTIFACT_RETENTION_DAYS`. Existing pre-v0.8.158 drafts
without `expiresAt` use their original creation time and the current policy.
Reducing retention takes effect on the next upload replay/reconciliation; increasing
retention cannot extend a new draft's original `expiresAt`. Expired requests
are refused, not silently recreated.

On the authorized host, verify the exact tenant, approved retention period and
current Action Ledger before running the dry-run. Example:

```sh
python3 scripts/reconcile-knowledge-uploads.py \
  --data-root /opt/data/.modelgarden --tenant acme \
  --actor operator:alice --retention-days 7
```

To apply the listed **expired** draft removals, repeat with
`--apply --confirm-tenant acme --audit-path /opt/data/.modelgarden/audit.jsonl`.
A separately approved individual erasure uses
`--erase-id <full-upload-id> --reason owner-request` (or `reconciliation`
for documented incident repair). The command requires existing host/operator
access; the `--actor` string is an audit attribution, **not** an authentication
mechanism or a substitute for client-owner approval. Do not expose this command
or the mounted data volume through public web chat.

The reconciler holds the same tenant-local lock as upload staging, verifies
tenant, metadata, content digest and regular-file boundaries, refuses symlinks,
and writes a redacted `knowledge.upload-delete-intent` receipt before deletion
and `knowledge.upload-deleted` afterward. A private minimal tombstone prevents
replay of an erased request ID. If the ledger is unavailable, deletion does not
start; if deletion or final audit fails, inspect the tombstone, content and
ledger before any manual repair. Never claim an interrupted deletion completed.
A dry-run never deletes files or writes ledger events. `pending-audit` drafts
require particular care: confirm whether the original staging receipt exists.

**Not covered:** no scheduled worker purge, retention of backups, imported
KnowledgeSources, external copies or immutable ledger export. The operator must
coordinate their independent retention/erasure procedures. This is credential-free
implementation evidence, not a live legal-erasure or client-acceptance drill.

## Reviewed staged upload to private draft (v0.8.200)

The opt-in signed owner review is now consumable by a **host-authorized
operator**, without copying an unverified staged file by hand. The command
refuses a missing/rejected/interrupted review, wrong owner digest, wrong tenant,
changed/expired content, linked or exposed files, and a workspace whose final
directory name is not the exact tenant slug. It reads the staged bytes under
the existing upload lock and uses the existing Tier 0 importer. The first run
is review-only and does not edit the workspace:

```sh
python3 scripts/import-reviewed-knowledge.py /private/workspaces/acme \
  --data-root /private/data --tenant acme \
  --upload-id <full-opaque-id> --expected-sha256 <reviewed-digest> \
  --owner owner@acme.example --actor operator:alice \
  --name business-rules --agent receptionist --classification internal \
  --retention-days 30
```

After independently verifying the client-owner identity, the exact digest,
client retention setting, workspace, and private Action Ledger, the operator
may repeat with `--apply --confirm-tenant acme --audit-path /private/data/audit.jsonl`.
An audit intent is written **before** the workspace import and a completion
receipt afterward; a failed completion receipt requires operator reconciliation.
The imported source remains **draft** and cannot ground `kb.search` until
the client separately approves the protected workspace diff, reruns the
appropriate evaluations and promotes the exact source through the existing
reviewed deployment process. The command does not authenticate the operator
or owner, deliver a notification, prove live SMTP, or authorize production.
