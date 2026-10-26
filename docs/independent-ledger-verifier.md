# Independent Action Ledger verifier (opt-in, #80)

`platform/core/ledger_export.py` verifies the exported hash chain, local-chain parity, historical heads and truncation. `scripts/ledger-verifier-health.py` is a **read-only, scheduled-verifier prerequisite**: on a separately operated verifier host it checks the client GCS export with a scoped impersonated read identity, emits a **redacted Cloud Logging level** after every poll, and exits nonzero on unhealthy results. It does **not** make a bucket immutable, deliver an alert to a person, or prove a real client deployment. A failed GCS read, expired credential, invalid verification report or broken chain must never become a healthy level. Errors and ledger content are not forwarded to Cloud Logging.

## Operator prerequisites and execution

1. Obtain client approval for the retention period, bucket retention **lock** (irreversible), owner and escalation policy. Apply the opt-in `ledger_export_enabled` configuration under an authorized change process; grant a **separately administered** verifier service account only `roles/storage.objectViewer` on that client's bucket. The runtime and deploy identities must not have that read role. Use an approved verifier host/runner distinct from the runtime and deploy host. Its caller must have narrowly scoped permission to impersonate the verifier identity, and the logging writer needs `logging.logEntries.create` for the named GCP project. Verify actual principal and IAM grants independently; the CLI's identity-name check is not identity attestation.
2. Provide a private verifier-owned `--state-dir` (mode `0700`) and read-only access to the correct client ledger snapshot. This snapshot must be authentic and current; a local file from an untrusted host cannot establish independent integrity. Configure a trusted sync or mount and retain evidence of its freshness. The verifier must **not** receive the runtime's export-writer identity. Never pass secrets as CLI flags.
3. Schedule the command on the **independent verifier host** every five minutes with an approved systemd timer or existing scheduler. A sample invocation (replace every placeholder with approved client values):

```bash
python3 scripts/ledger-verifier-health.py \
  --ledger /srv/verifier/acme-dev/audit.jsonl \
  --state-dir /var/lib/model-garden-verifier/acme-dev \
  --gcs-bucket CLIENT_RETENTION_LOCKED_BUCKET --gcs-prefix acme-dev \
  --client-project acme-dev --confirm-client-project acme-dev \
  --gcp-project client-project-123 --confirm-gcp-project client-project-123 \
  --verifier-identity auditor@client-project-123.iam.gserviceaccount.com \
  --runtime-identity runtime@client-project-123.iam.gserviceaccount.com
```

Use a locked-down systemd service (`User=`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `PrivateTmp=yes`, writable state directory only) and timer (`OnBootSec=5min`, `OnUnitActiveSec=5min`, `Persistent=true`), with the exact command and host-specific paths reviewed by the operator. This repository does **not** install or enable the timer or grant cloud permissions automatically. Capture the real timer state and first GCS read/Cloud Logging receipt in the client evidence record.

## Optional client-isolated Cloud Monitoring alert

`infra/docker-host/gcp/ledger-verifier-health.tf` creates two client-scoped log-based metrics (unhealthy samples and all heartbeat samples) and an OR alert for unhealthy levels or a missing heartbeat for 15 minutes. It is **off by default**. Only set `ledger_verifier_alert_enabled=true` after authorized export configuration, an independent verifier member, an approved same-project notification channel and a **real first verifier sample**. The `gateway_uptime_notification_channels` list is reused; no new public ingress or external SaaS is introduced. The missing-sample condition is necessary because a crashed or credential-blocked verifier cannot publish its own failure. Confirm the log-based metric is populated and the alert actually fires and reaches an operator; Terraform plan success and `gcloud logging write` success are not delivery proof.

The command logs only `clientProject`, `health`, bounded condition names and explicit `notificationsDelivered=false`; it never logs ledger events, paths, token values, provider error text, recipient addresses or customer content. Its local `ExportMonitor` state records due notices but **does not mark them delivered** when a Cloud Logging write succeeds. Cloud Monitoring owns notification delivery and escalation. Failed logging returns a redacted nonzero error; the missing-heartbeat alert is the independent failure signal. Retained live evidence must show the retention lock, distinct principal permissions, exported record/head receipts, chain/truncation verification, failure/recovery and actual notification delivery before closing #80.
