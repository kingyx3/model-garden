# Preapproved recurring client backup dispatch (#230)

This **opt-in, offline-testable** dispatcher is designed to be invoked by an existing, operator-managed host timer (for example, a systemd timer every minute). It does **not** install or enable a timer, grant cloud permissions, stop services, automatically retry, authorize itself, or prove a recovery. Do not enable it on a live client until the client approves the recurrence, every occurrence, and an independently enforced stopped-project/deployment freeze.

## Two levels of client authority

Use an authorized host operator UID and create private, UID-owned directories (0700) for the recurrence policy, occurrence approvals and durable claims. All JSON files must be regular, owned by that UID, mode 0600, without symlinks or hardlinks. Store actual client authorization and operator/change evidence in the existing change-management system; local JSON is an approval **claim**, not cryptographic proof.

A recurrence policy (maximum 31 days) binds the client, environment, GCP project, public age-recipient SHA-256 fingerprint, operator and change reference, and UTC weekdays/time. The weekday numbering follows Python: Monday=0 through Sunday=6. For example, /private/acme/policy/schedule.json:

~~~json
{
  "schemaVersion": 1,
  "clientProject": "acme-dev",
  "clientSlug": "acme",
  "environment": "dev",
  "gcpProjectId": "sample-project-123",
  "ageRecipientSha256": "REPLACE_WITH_LOWERCASE_SHA256_OF_PUBLIC_RECIPIENT",
  "approvedBy": "operator-1",
  "changeRef": "CHG-123",
  "validFrom": "2026-10-23T00:00:00Z",
  "expiresAt": "2026-11-15T00:00:00Z",
  "weekdaysUTC": [0, 2, 4],
  "hourUTC": 12,
  "minuteUTC": 0
}
~~~

**Each scheduled occurrence additionally needs a distinct existing, 0600 private approval** with the exact v1 fields documented in [client-backup-window.md](client-backup-window.md). Its validFrom must be exactly the UTC slot start, expiresAt must be within 60 minutes of that start, and approvedBy/changeRef must match the recurrence. For the Friday 2026-10-23 12:00 UTC slot, place it at /private/acme/approvals/acme-dev-20261023T120000Z.json. Do not pre-create approvals without client authority. Missing, expired, cross-client, wrong-key or wrong-slot approvals **refuse execution**.

## Operator-managed timer invocation

Use a separately reviewed timer that invokes the following command at least once in the first five minutes of each approved slot; a once-per-minute systemd timer is suitable. A call outside the scheduled UTC weekday and five-minute grace interval skips without cloud work. The five-minute grace may cross UTC midnight (for example, Friday 23:59 through Saturday 00:03:59). The dispatcher checks the **slot's UTC weekday and policy bounds**, not the timer invocation date; the approval filename, durable claim and receipt retain the prior UTC slot date. At exactly five minutes after the slot, execution skips. **The recurring policy must still be unexpired at the actual timer invocation**: if `expiresAt` falls inside the five-minute grace, execution at or after that instant skips without a claim or upload, even when the per-slot approval remains valid. This also applies when the grace crosses UTC midnight. The previous-day slot is never accepted without its own matching, still-active approval. Run only as the same dedicated host UID that owns the private directories and the existing /run/lock/model-garden cooperative project lock. No timer or production service is installed by this repository change.

~~~bash
python3 scripts/client-backup-schedule.py \
  --schedule /private/acme/policy/schedule.json \
  --approvals /private/acme/approvals \
  --claims /private/acme/claims \
  --compose /trusted/acme/compose.yaml \
  --project acme-dev --client-slug acme --environment dev \
  --gcp-project-id sample-project-123 \
  --recipient "$APPROVED_PUBLIC_AGE_RECIPIENT" \
  --image 'docker.io/library/busybox@sha256:REPLACE_WITH_PINNED_DIGEST' \
  --output-parent /private/acme/backups --confirm-project acme-dev
~~~

The dispatcher pins the recurrence policy file and directory identity and revalidates the exact policy at dispatch and **before inventory, snapshot, upload and successful completion**. Deleting, modifying or atomically replacing the policy (even with identical JSON), changing its directory, or revoking its private permissions fails closed; an attempted slot remains consumed and receives a failed/unknown receipt, never a false success. The existing single-occurrence approval is independently revalidated at those boundaries. This is cooperative, host-local **stage-boundary revocation**, not cancellation of an already accepted Docker/GCS operation, protection against privileged host compromise, or a cross-host deployment freeze.

The dispatcher passes the recurrence's `expiresAt` to the bounded backup-window runner as an additional **hard execution deadline**. The shorter of the single-occurrence approval and recurrence expiry limits every Docker, snapshot and upload step; a recurrence with under 30 seconds left refuses before starting. A monotonic deadline prevents a wall-clock rollback from extending the operation. This is a process-level deadline, **not** an independently enforced deployment freeze, guaranteed cloud cancellation, or live recovery evidence. A failed/expired run consumes its slot claim and requires new authorization; never retry automatically.

The dispatcher revalidates the exact approval, then atomically creates and fsyncs a per-client/per-slot **attempted** claim (0600) before calling the existing bounded client-backup-window.py runner. A duplicate attempt is refused even after an error or host restart. **Do not delete claims to retry.** Inspect possible incomplete ciphertext and GCS objects, obtain a fresh authorized window, and use a new slot. The underlying runner still checks stopped Compose containers, locks the project, uses age-encrypted snapshots and create-only GCS upload, and emits redacted JSON events.

After the irreversible `.claimed` record, the dispatcher also writes a **separate, owner-private, fsynced** `.result.json` for that client and UTC slot. Its only terminal states are `uploaded-not-recovery-proven` (bounded upload runner returned success) and `failed-not-recovery-proven` (runner rejected or failed). Receipts contain only the project, slot and outcome, never ciphertext, secret material, approval text or exception details. Existing receipt paths, including symlinks, are never overwritten. A missing, truncated or damaged result is **UNKNOWN** and must be investigated; it never authorizes replay. An irreversible claim still blocks the slot even if receipt persistence fails. These local status files help an external approved monitoring system classify outcomes; they do not send notifications themselves or constitute immutable evidence.

A skipped slot is not a successful backup. backup.schedule.dispatched means the upload step returned, **not** that independent remote read-back or restore succeeded. A failure emits backup.schedule.failed with no credentials, payloads or exception text. Operators must monitor timer failures and missed slots; deduplicated client-channel failure/recovery alert delivery remains unimplemented.

## Review-only systemd timer bundle

`scripts/render-client-backup-units.py` renders a client/environment-specific oneshot service and once-per-minute calendar timer as **JSON for review only**. It neither writes unit files nor installs, enables, starts or deploys anything. This is a bounded code prerequisite for scheduling, **not** evidence that a backup occurred. The timer polls; the existing dispatcher still requires both the finite client-approved recurrence and a separate valid approval for each exact slot, with a durable no-retry claim. `Persistent=true` may catch up a missed timer tick but cannot bypass those checks.

Example for an authorized DEV operator (substitute a real public age recipient and a reviewed SHA-256-pinned helper image):

~~~bash
python3 scripts/render-client-backup-units.py \
  --client-slug acme --environment dev \
  --client-project acme-dev --confirm-client-project acme-dev \
  --gcp-project acme-gcp-123 --confirm-gcp-project acme-gcp-123 \
  --service-user mg-backup \
  --age-recipient "$APPROVED_PUBLIC_AGE_RECIPIENT" \
  --snapshot-image "$PINNED_SNAPSHOT_IMAGE" \
  > /private/review/acme-dev-backup-units.json
~~~

Before **separately approved** installation, inspect both unit strings and validate them with `systemd-analyze verify`. The renderer fixes its paths per tenant: `/srv/model-garden/<project>/compose.yaml` (reviewed read-only Compose) and `/var/lib/model-garden-backup/<project>/` with `policy/schedule.json`, `approvals/`, `claims/` and `snapshots/`. Provision all directories manually as private (0700) and owned by the dedicated operator UID; policy/approval files must be 0600 and owned by that UID. The separate cooperative lock directory `/run/lock/model-garden` must exist with the reviewed access policy. The timer service does **not** create these paths, consent documents, locks or credentials.

The unit has a fixed interpreter/script path, no shell, no `EnvironmentFile`, a pinned snapshot image, explicit tenant/GCP confirmations, restricted writable paths and a bounded service lifetime. Its only embedded key is the **public** age recipient. Review the service user's Docker socket permissions carefully: Docker access can be effectively root-equivalent, and `NoNewPrivileges=yes` is not a substitute for a trusted, isolated host. Prefer the existing approved keyless GCP host identity; never add a cloud key or OAuth token to a unit, command line or client repository. Verify the service can read the Compose file and approved private paths without broadening permissions. **Do not enable the timer** until client owner authorization, external deployment/restart freeze, least-privilege IAM, key custody and alert delivery are independently verified.

Local credential-free tests cover tenant/project isolation, explicit confirmations, service/user injection, pinned image validation, review-only output and redacted refusal. The timer expression and rendered unit syntax were also checked locally with `systemd-analyze`. Neither those tests nor a generated unit proves an installed timer, an uploaded object, independent read-back, a successful restore, or delivered notification.

## Remaining safety gates

The host-local cooperative lock does **not** prevent out-of-band Docker commands, container restart policies, other hosts or watchdogs. An independently enforced quiescence/freeze is mandatory. This dispatcher does not establish key rotation, live cloud recovery evidence, RPO/RTO, or automatic retries. Issue #230 stays open until those gates are completed and a client-authorized real restore drill is recorded.

## Key rotation readiness

Before changing the scheduled public age recipient, follow [the operator-only retained-snapshot rotation preflight](client-backup-key-rotation.md). It authenticates the **provided** private read-backs under the old identity and refuses missing or cross-tenant material; it does not enumerate GCS or authorize retiring the old identity. Fresh client approvals, a new-key backup/read-back/restore drill, and old-key retention remain mandatory.
