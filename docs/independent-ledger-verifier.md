# Independent Action Ledger verifier (opt-in, #80)

`platform/core/ledger_export.py` verifies the exported hash chain, local-chain parity, historical heads and truncation. `scripts/ledger-verifier-health.py` is a **read-only, scheduled-verifier prerequisite**: on a separately operated verifier host it checks the client GCS export with a scoped impersonated read identity, emits a **redacted Cloud Logging level** after every poll, and exits nonzero on unhealthy results. It does **not** make a bucket immutable, deliver an alert to a person, or prove a real client deployment. A failed GCS read, expired credential, invalid verification report or broken chain must never become a healthy level. **An empty/missing local ledger paired with an empty export is not successful verification:** the health wrapper now requires positive, integer local chained-event, exported-record and exported-head-sequence evidence before publishing `HEALTHY`. An empty or malformed verification report emits a redacted `UNHEALTHY`/`export-failure` level; it does not certify a newly provisioned tenant. This is still a local-report consistency guard, not proof of an independently authenticated snapshot, retention lock, or delivered alert. Errors and ledger content are not forwarded to Cloud Logging.

## Operator prerequisites and execution

1. Obtain client approval for the retention period, bucket retention **lock** (irreversible), owner and escalation policy. Apply the opt-in `ledger_export_enabled` configuration under an authorized change process; grant a **separately administered** verifier service account only `roles/storage.objectViewer` on that client's bucket. The runtime and deploy identities must not have that read role. Use an approved verifier host/runner distinct from the runtime and deploy host. Its caller must have narrowly scoped permission to impersonate the verifier identity, and the logging writer needs `logging.logEntries.create` for the named GCP project. Verify actual principal and IAM grants independently; the CLI's identity-name check is not identity attestation.
2. Provide a private verifier-owned `--state-dir` (mode `0700`) and read-only access to the correct client ledger snapshot. This snapshot must be authentic and current; a local file from an untrusted host cannot establish independent integrity. Configure a trusted sync or mount and retain evidence of its freshness. The verifier must **not** receive the runtime's export-writer identity. Never pass secrets as CLI flags.
3. Schedule the command on the **independent verifier host** every five minutes with an approved systemd timer or existing scheduler. A sample invocation (replace every placeholder with approved client values):

```bash
python3 scripts/ledger-verifier-health.py \
  --ledger /srv/verifier/acme-dev-mg/audit.jsonl \
  --state-dir /var/lib/model-garden-verifier/acme-dev-mg \
  --gcs-bucket CLIENT_RETENTION_LOCKED_BUCKET --gcs-prefix acme-dev-mg \
  --client-project acme-dev-mg --confirm-client-project acme-dev-mg \
  --gcp-project client-project-123 --confirm-gcp-project client-project-123 \
  --verifier-identity auditor@client-project-123.iam.gserviceaccount.com \
  --runtime-identity runtime@client-project-123.iam.gserviceaccount.com
```

Use a locked-down systemd service (`User=`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `PrivateTmp=yes`, writable state directory only) and timer (`OnBootSec=5min`, `OnUnitActiveSec=5min`, `Persistent=true`), with the exact command and host-specific paths reviewed by the operator. This repository does **not** install or enable the timer or grant cloud permissions automatically. Capture the real timer state and first GCS read/Cloud Logging receipt in the client evidence record.

## Optional client-isolated Cloud Monitoring alert

`infra/docker-host/gcp/ledger-verifier-health.tf` creates two client-scoped log-based metrics (unhealthy samples and all heartbeat samples) and an OR alert for unhealthy levels or a missing heartbeat for 15 minutes. It is **off by default**. Only set `ledger_verifier_alert_enabled=true` after authorized export configuration, an independent verifier member, an approved same-project notification channel and a **real first verifier sample**. The `gateway_uptime_notification_channels` list is reused; no new public ingress or external SaaS is introduced. The missing-sample condition is necessary because a crashed or credential-blocked verifier cannot publish its own failure. Confirm the log-based metric is populated and the alert actually fires and reaches an operator; Terraform plan success and `gcloud logging write` success are not delivery proof.

The command logs only `clientProject`, `health`, bounded condition names and explicit `notificationsDelivered=false`; it never logs ledger events, paths, token values, provider error text, recipient addresses or customer content. Its local `ExportMonitor` state records due notices but **does not mark them delivered** when a Cloud Logging write succeeds. Cloud Monitoring owns notification delivery and escalation. Failed logging returns a redacted nonzero error; the missing-heartbeat alert is the independent failure signal. Retained live evidence must show the retention lock, distinct principal permissions, exported record/head receipts, chain/truncation verification, failure/recovery and actual notification delivery before closing #80.

## Reviewable independent-host systemd units (v0.8.170)

For a repeatable five-minute poll, an operator can generate **review-only** systemd service and timer text with `scripts/render-ledger-verifier-units.py`. The generator validates two explicit client/GCP project confirmations, a client-prefixed export namespace, distinct verifier/runtime service-account names and an unprivileged local user. It never connects to GCP, installs a unit, enables a timer or reports notification delivery. The generated service executes the existing independent verifier without a shell, runs with a private home/state directory, has read-only access to the named ledger snapshot and a writable per-client state directory only.

```sh
python3 scripts/render-ledger-verifier-units.py \
  --client-project acme-dev-mg --confirm-client-project acme-dev-mg \
  --gcp-project client-project-123 --confirm-gcp-project client-project-123 \
  --bucket CLIENT_RETENTION_LOCKED_BUCKET --prefix acme-dev-mg \
  --verifier-identity auditor@client-project-123.iam.gserviceaccount.com \
  --runtime-identity runtime@client-project-123.iam.gserviceaccount.com \
  --service-user mg-verifier > reviewed-verifier-units.json
```

On the **separately administered** verifier host, the authorized operator must review and install the two unit texts from the JSON under `/etc/systemd/system/`, create the dedicated unprivileged user/group, ensure `/opt/model-garden` contains the pinned release, provision `/srv/model-garden-verifier/acme-dev-mg/audit.jsonl` as an authentic current read-only ledger snapshot, and create `/var/lib/model-garden-verifier/acme-dev-mg` owned by the service user with mode `0700`. The service assumes metadata-based identity access on that host; verify the **actual** principal, impersonation grant, separate read-only bucket IAM and logging permissions before any timer enablement. The unit intentionally cannot write to the runtime ledger, export bucket or other clients' state.

After the required client/operator/security approvals, use `systemd-analyze verify` on the reviewed files, then enable only the timer under the host's change procedure. Observe at least one real successful GCS read, a missing-heartbeat failure, a corrupted/truncated-chain failure and recovery, and the actual Cloud Monitoring recipient notification receipts. The timer's presence or a green synthetic test does not establish storage immutability, trusted snapshot freshness, independently enforced identity separation, or delivered alerts. Revoke/disable the timer and associated IAM through the approved retirement process.

## Independently pinned exported-chain readback (v0.8.216)

A separately administered, read-only verifier can now check an exported chain **without any copy of the runtime ledger**, provided the security owner has independently retained a trusted head witness (sequence and SHA-256) **outside the export bucket and runtime host**. Use this only as a supplemental integrity check; the existing `ledger-verifier-health.py` still requires a trusted current local snapshot to test local/export parity and detect local truncation.

```sh
python3 scripts/ledger-export.py verify-remote \
  --gcs-bucket CLIENT_RETENTION_LOCKED_BUCKET --gcs-prefix APPROVED_CLIENT_NAMESPACE \
  --gcs-impersonate APPROVED_READ_ONLY_VERIFIER_IDENTITY \
  --expected-head-sequence 123 --expected-head-hash INDEPENDENTLY_RETAINED_64_HEX_DIGEST
```

The command fails closed on a missing or different pinned head, missing/extra exported sequences, altered event/schema/hash/linkage, inconsistent historical head manifests, or missing first chained record. It emits only counts, a bounded integrity status and the explicit scope `exported-chain-only`. For an older ledger with unexported v1 records preceding the first chained event, the owner must independently establish `--expected-first-sequence`; `legacyPrefixVerified=false` records that those v1 records are **not** proven by this mode. Never infer the witness from the same bucket being verified: that would not independently anchor the result.

A successful result has `localComparisonPerformed=false` and `localTruncationProven=false`. It does **not** prove the local ledger was not truncated, that the newest event was exported, the trustworthiness of the head witness, retention lock, separate IAM, client authorization, actual alert delivery, or operational acceptance. Run the original `verify` mode with a trusted current local snapshot and retain real GCS object, identity and alert receipts for #80. No cloud apply or verifier installation is performed by this feature.
