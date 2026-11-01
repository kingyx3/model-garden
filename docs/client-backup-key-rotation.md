# Client backup age-recipient rotation preflight (#230)

**Operator-only, offline, read-only prerequisite.** The existing stopped-Compose
snapshot/upload/read-back/restore pipeline remains authoritative. This command
does not rotate credentials, modify GCS, install a timer, stop/start services,
approve a new key, or demonstrate disaster recovery.

Before changing the recipient in a client-approved backup recurrence, obtain an
**independent inventory** of all GCS backups still covered by retention or legal
hold, with source bucket/project, object generations, retention dates and
authorized read identity. Download every retained run through the existing
`client-backup-readback.py` into a private (0700), operator-owned directory;
each run directory must contain its complete 0600 manifest and ciphertext.
Do not use this tool to claim that a local directory represents all cloud data.

On a trusted, isolated operator host, obtain the **old** age identity from
approved offline custody. Keep it private (0600), owner-owned, and never place
it in the repo, CI, a command argument, GCS, an issue or a log. Review a distinct
new **public** age recipient under the client's change approval.

```sh
python3 scripts/client-backup-key-rotation.py \
  --compose /trusted/acme-dev/compose.yaml \
  --project acme-dev \
  --snapshots /private/acme-dev/retained-readbacks \
  --old-recipient "$OLD_PUBLIC_AGE_RECIPIENT" \
  --new-recipient "$NEW_PUBLIC_AGE_RECIPIENT" \
  --old-identity /private/offline-custody/old-age-identity
```

The preflight refuses empty, unexpected, linked, foreign-owned, permissive or
oversized local material, wrong tenant/recipient fingerprints, incomplete
Compose volume inventories, altered ciphertext, and failed authenticated age
decryption/TAR safety checks. It reuses `client-volume-restore.py verify` for
every supplied run, and detects manifest changes during verification. Output
contains only fingerprints, counts and explicit **non-proof** status; errors
never echo keys, source paths or customer payloads.

A passing result means **only** that the supplied local old-key snapshots were
authenticated and could be read by the retained identity. It is not independent
proof of complete cloud inventory, old-key escrow durability, authorized
recipient change, new-key decryptability, a replacement-host restore, or
measured RPO/RTO. Keep the old identity recoverable for the full retention/hold
period, even after new backups use the new recipient. A recipient change
requires a fresh, independently signed client approval, a new recurrence and
per-slot approvals bound to the new recipient fingerprint, a new encrypted
snapshot, independent cloud read-back, and a replacement-host restore drill
using the **new** key. Do not silently rewrite historical GCS ciphertext or
delete/revoke the old identity. Maintain independent restart/deploy exclusion
and backup/alert monitoring throughout.

Run the credential-free tests with
`python3 -m unittest tests.test_client_backup_key_rotation -v`.
Issue #230 remains open until approved live cloud backup/restore, alert delivery,
key custody and observed RPO/RTO evidence are independently retained.

## Separate recovery-reader identity (v0.8.263)

When the opt-in backup bucket is provisioned, Terraform also creates a
**distinct read-only recovery service account**, exposed as
`runtime_backup_reader_service_account`. The reader has
`roles/storage.objectViewer` **only on that client/environment's bucket**;
the backup writer retains create-only access. Terraform deliberately does
**not** grant the runtime, deployer or backup host permission to impersonate
the reader. The objectViewer role includes bucket-local listing; use a
separately administered client account and restrict operator access.

`scripts/client-backup-readback.py` now **requires** gcloud impersonation of
the exact Terraform-derived reader identity on every cloud describe/copy.
It does not accept an arbitrary reader flag and cannot fall back to ambient
operator credentials. An authorized client security owner must separately
grant a recovery operator `roles/iam.serviceAccountTokenCreator` on that
specific reader account, with approved duration and revocation. This is
**not** a grant to the runtime, nor permission to decrypt age ciphertext.
Run recovery from a trusted isolated host with independent age-key custody.

**Migration:** Existing opt-in installations must review/apply the Terraform
reader addition and approve operator impersonation **before** upgrading
readback automation. Until then, readback intentionally fails closed.
Reconcile GCS Data Access logs, object generations, retained manifests and
independent restore evidence; IAM provisioning or mocked gcloud tests do
not prove a live readback, key custody, replacement-host restore or RPO/RTO.
Do not run Terraform against a client environment without authorization.

## Restore-time manifest integrity

The operator restore CLI now pins the original private manifest inode and
contents **before** authenticated archive verification, then rechecks it
before and after each Docker volume extraction. It compares ciphertext
against the digest/size from that original manifest, not a newly read manifest
that another same-UID process could replace during the recovery window.
Any detected change fails closed; if a change is detected after extraction,
treat the destination as **partially restored**, keep the project stopped and
restart the approved recovery process with a fresh empty destination.

This is a local time-of-check/time-of-use safeguard, **not** an immutable
remote source attestation or a distributed deployment/restart freeze. Use a
trusted isolated recovery host, protect the source directory from concurrent
writers, independently verify GCS object generations and retained receipts,
and conduct the client-authorized replacement-host recovery drill. No live
restore, measured RPO/RTO or key rotation is implied by CI.
