# Bounded client-approved backup window (#230)

This operator-run command executes **one** encrypted snapshot of a **stopped** client Compose project, then calls the existing create-only GCS uploader. It does **not** schedule backups, stop/restart services, change IAM, decrypt archives, automatically retry partial transfers, or prove live recovery.

## Preconditions and authority

Obtain client approval for the exact client/environment, GCP project, public age recipient, named operator, change reference and a maximum **60-minute** UTC window. Externally enforce a deployment/restart freeze, including watchdogs, auto-restart policies and other operators. The backup runner, single-client Docker apply and reference voice overlay apply now share a **cooperative, host-local** project lock. This blocks concurrent invocations through those two entrypoints but cannot prevent Docker restart policies, direct Docker commands, other deployers, or other hosts. Do not run on live data without independently enforced quiescence.

Use an authorized operator host with Docker, age, gcloud, a pinned digest-only tar helper, approved tenant/environment-isolated create-only GCS writer and trusted Compose file. Keep the age private identity offline. The existing snapshot and upload tools check full named-volume inventory, Docker ownership labels, encrypted hashes and manifest-last publication.

Create an existing private directory (0700), and a private approval JSON file (0600), with **exactly** these keys and no private keys:

~~~json
{
  "schemaVersion": 1,
  "clientProject": "acme-dev",
  "clientSlug": "acme",
  "environment": "dev",
  "gcpProjectId": "sample-project-123",
  "ageRecipientSha256": "REPLACE_WITH_SHA256_OF_PUBLIC_AGE_RECIPIENT",
  "approvedBy": "operator-1",
  "changeRef": "CHG-123",
  "validFrom": "2026-10-23T08:00:00Z",
  "expiresAt": "2026-10-23T08:30:00Z"
}
~~~

The recipient fingerprint is the lowercase hex SHA-256 of the ASCII public age recipient string, not the private identity. Timestamps must be UTC seconds, current, at most 3600 seconds apart and leave at least 30 seconds. This local file records an approval **claim**, not independent cryptographic authorization: enforce host RBAC and retain the real client approval in the existing change-management system.

## Host lock setup (required for backup, single-client deploy apply and voice overlay apply)

On the same Docker host, pre-provision `/run/lock/model-garden` as a **0700 directory owned by the dedicated Unix operator UID** used by all three commands. The lock files are created as 0600 and never unlinked; the commands fail closed when the directory is absent, symlinked, group-accessible, or owned by another UID. Ensure both processes use the same host mount namespace and operator identity; the directory under `/run` must be recreated securely after each reboot. Do not mount separate lock directories or bypass this guard with a different deployment entrypoint. This is a cooperative safeguard only; independently stop auto-restarts and enforce the client-approved maintenance freeze before running backup or restore.

## Execute once during the approved stopped-project window

~~~bash
python3 scripts/client-backup-window.py \
  --compose /trusted/client/compose.yaml \
  --project acme-dev --client-slug acme --environment dev \
  --gcp-project-id sample-project-123 \
  --recipient "$APPROVED_PUBLIC_AGE_RECIPIENT" \
  --image 'docker.io/library/busybox@sha256:REPLACE_WITH_PINNED_DIGEST' \
  --approval /private/backup/approval.json \
  --output-parent /private/backup --confirm-project acme-dev
~~~

When called by the recurring dispatcher, the runner also accepts an internal UTC `not_after` bound equal to the recurrence's expiration. It refuses an expired or under-30-second recurrence and caps the subprocess deadline at the earlier of the per-occurrence approval and recurrence expiry. Direct operator CLI use remains governed by its explicit single-window approval. This does not independently freeze other deployment actors.

The runner validates the approval and private files, takes an exclusive per-client lock, verifies the stopped project and Compose inventory, creates a unique snapshot run ID, executes age-encrypted snapshot then create-only upload once, checks quiescence between stages and kills the process group when the approved deadline expires. The backup-window runner now also inspects **all existing Compose-labelled containers** (including exited and created containers) before and between stages, requires exact project labels and stable Docker IDs, rejects active/transitional states and **any restart policy other than `no`**. This is a read-only, bounded fail-closed guard: operators must disable restart policies under an approved change and independently freeze systemd/watchdogs, direct Docker access and all other hosts. Docker inspections are not an atomic guarantee against a restart immediately afterward. It emits redacted JSON events backup.window.completed or backup.window.failed; a completed event means **uploaded-not-recovery-proven**. An interrupted transfer may leave local ciphertext or remote objects without a completion manifest. Never overwrite or automatically retry a partial run; review it under the same client change control. Keep the deployment freeze until an authorized operator reconciles state.

## Still open

This is **not** a timer or a complete deployment-wide restart exclusion mechanism. The shared host lock now coordinates this backup runner, single-client Docker apply and reference voice overlay apply; direct Docker operations, other hosts and external automation still require independent controls. The window's read-only restart-policy inspection refuses unsafe existing containers but cannot prevent out-of-band changes after inspection or detect an unlabelled watchdog. Before unattended scheduling, implement authorized recurrence, full restart exclusion, bounded failure retry, deduplicated failure/recovery alerts to the client-approved channel and key rotation governance. A successful CLI exit is not an independent GCS read-back, replacement-host restore, application-integrity check or live RPO/RTO drill. Those remain acceptance gates in issue #230.

## Same-daemon named-volume attachment exclusion (v0.8.97)

The underlying `client-volume-snapshot.py` now additionally checks **every declared, Compose-owned named volume** for attached containers on the same Docker daemon, including stopped containers. It refuses unlabelled/foreign-project attachments, live or transitional containers, any restart policy other than `no`, malformed/duplicate/unbounded container inventories and failed Docker inspections. It rechecks each volume before and after streaming and rechecks the complete set before publishing the manifest. The guard reads only container IDs, Compose project labels, states and restart policy; it never reads container environment or client payloads. Failures do not produce a success manifest. This also protects direct operator snapshots that bypass the backup-window wrapper.

**Scope limit:** Docker volume-filter inspection is a point-in-time, single-daemon check. It does **not** prevent a new attachment immediately afterward, prove absence of remote writers to shared storage, stop a watchdog, block other hosts or establish independently enforced deployment-wide quiescence. Continue to require a client-approved external maintenance freeze, authorized timer activation and live restore evidence. Do not treat offline tests as proof of cloud/tenant readiness.

## Restore-side attachment guard (v0.8.101)

The operator-only `client-volume-restore.py restore` now applies the existing same-daemon, fail-closed volume-attachment inspection to **every target volume** before any writes, immediately before extraction, after each extraction and at the end. It rejects foreign/unlabelled, running, transitional or restartable Docker containers attached to the target, including stopped containers. An attachment detected after extraction is a **failed, potentially partial restore**: keep the stack stopped and investigate; never automatically retry, overwrite or start it. The helper container used to inspect emptiness or extract data is short-lived and removed before the subsequent inspection.

This is **not** an atomic or cross-host maintenance freeze: a privileged actor, another host or a watchdog can still attach after inspection. Operators must independently enforce the client-approved deployment freeze, review restored state and conduct a real measured replacement-host drill before claiming recoverability. Offline tests do not establish live proof.

## Stage-boundary approval revocation (v0.8.110)

The window runner now pins the private approval file's device/inode and size/mtime/ctime before work starts. It refuses linked or foreign-owned approvals, and revalidates the exact tenant, recipient, operator/change reference and active window before inventory, snapshot, create-only upload and completion. Removing, editing or atomically replacing the approval file (even with identical JSON) fails closed at the next stage boundary. This protects against **local approval revocation between stages**, not a distributed lease: a revocation during a long-running subprocess cannot cancel an already accepted cloud write, and an upload may have succeeded even if the final completion check fails. Reconcile the private receipt and remote manifest under change control; never blindly retry. The host-local project lock, external deployment freeze, separately retained client authorization and real restore drill are still required.

## Host operation lock acquisition identity (v0.8.111)

The shared host-local project lock now rechecks the operator-owned 0700 lock directory and the exact 0600 project-lock inode **after** acquiring `flock` and **before** entering protected backup/deploy work. A directory swap, lock-file replacement, unlink, or permission/owner change during acquisition fails closed. Credential-free adversarial tests force these races.

This is **not** a distributed or durable freeze: a privileged actor can still replace paths after the check, bypass the cooperative entrypoints, or restart containers directly. Maintain the independently authorized deployment-wide maintenance exclusion and do not claim real recovery until the replacement-host RPO/RTO drill succeeds.


## Held host-lock lease revalidation (v0.8.115)

The cooperative per-project lock now returns a read-only lease check that pins the directory and lock-file identities, owner and private permissions throughout the operation. The backup window calls this check after each stopped-container inspection and before **inventory, encrypted snapshot, create-only upload and success acknowledgement**. A detached/replaced/unlinked or chmod-weakened lock fails closed at the next stage boundary; the context also checks at normal exit. Offline adversarial tests replace the lock directory after inventory and the lock file after snapshot, and assert that no subsequent write stage executes.

**Limitations:** the lease is host-local, cooperative and sampled only at stage boundaries. It cannot prevent a privileged same-host actor from racing between checks, cancel an in-flight snapshot/cloud write, stop direct Docker/restart policies or coordinate other hosts. A failed post-upload check may mean an upload occurred without a success acknowledgement; inspect immutable receipts and manifests before deciding on recovery. Client-approved independent deployment-wide quiescence and a measured live restore drill remain mandatory.

## Restore archive path uniqueness (v0.8.249)

Before writing to an empty target volume, the offline restore verifier now rejects TAR archives with repeated canonical member paths (including `./name` aliases) or regular files conflicting with descendant paths, in addition to existing symlink, traversal, member-count and size checks. A real snapshot's `./` root and ordinary nested directories remain valid. This prevents a malformed but digest-matching encrypted archive from producing order-dependent overwrites or partial extraction. It does **not** authenticate a backup's source independently, prevent post-verification privileged tampering, or establish a live restore/RPO/RTO. Continue to require a stopped, authorized client and independently verified replacement-host drill.
