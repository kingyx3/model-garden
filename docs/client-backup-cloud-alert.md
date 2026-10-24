# Client-scoped backup failure and recovery-transition alerts (#230)

`infra/docker-host/gcp/backup-health-alert.tf` adds two **default-disabled**
Cloud Monitoring log-match policies that consume the redacted
[`client-backup-cloud-log.py`](client-backup-cloud-log.md) samples. They reuse
native Cloud Monitoring incidents, channel delivery and hourly rate limiting;
there is no custom connector, shared alert bus or new IAM grant.

## Approval and activation boundary

Only an authorized operator may apply this Terraform after confirming the
client's isolated GCP project, backup bucket, verified notification-channel
ownership and agreed incident escalation owner. Set:

- `runtime_backup_enabled=true` for the same client/environment.
- `backup_health_alert_enabled=true` (default is **false**).
- `backup_health_notification_channels=["projects/PROJECT/notificationChannels/ID"]`
  using 1–16 distinct, existing, verified channels from **that same GCP project**.

The configuration rejects missing buckets/channels, duplicate channels,
malformed/cross-project channel references and malformed project IDs.
It creates **no** notification channels, IAM roles, timers or host agents.
Do not apply or emit live samples without client authorization. The Cloud
Logging adapter itself remains opt-in and must be run under an existing
project-scoped logging writer with a bounded approved polling schedule.

## Incident semantics and limitations

Both policies require the exact GCP log name, `global` resource, schema 1,
`client-backup` component and `client-slug/environment` binding.

- **Failure**: `UNHEALTHY` at ERROR severity. Repeated level samples can
  sustain visibility if an earlier transition was lost. Native log-match
  notification rate limiting is **one notification per hour per policy**.
- **Recovery transition**: `HEALTHY` at INFO severity **and**
  `backup.incident.recovered`. Ordinary healthy polls never generate a
  recovery notification. The incident monitor must observe a later healthy
  upload slot and persist its transition before this signal exists.

Both policies request only `OPENED` notifications. A log-match policy
**cannot automatically resolve the separate failure incident based on a
HEALTHY log**. Cloud Monitoring may auto-close a failure incident after
seven days (recovery-transition notice after 30 minutes); neither a
`CLOSED` event nor a recovery-transition notice is proof of restored
customer data. Operators must explicitly reconcile incident state and
verify the latest encrypted backup, independent read-back and an approved
restore drill. Rate limits, Cloud Monitoring quotas, delayed ingestion,
channel delivery failures, log-writer failures and missing polls can cause
lost or delayed notifications; **no silence-is-healthy assumption** is safe.
The monitor's private durable state remains authoritative for backup-health
transitions, not the Cloud Monitoring incident lifecycle.

**Offline checks:** `terraform -chdir=infra/docker-host/gcp test` uses a
mock Google provider to verify default-off behavior, isolation, malformed
inputs, filter bindings, deduplication settings and both transition types.
Mock plans do not prove live log ingestion, notification ownership,
delivery, provider quotas, or successful restoration.

**Live acceptance (not completed by this PR):** with explicit client
authorization, verify both alert policies in the exact project, deliver
controlled unhealthy and recovered events through the real operator
channels, record timestamps/acknowledgements and check missing-poll
escalation separately. Keep #230 open for independent restart exclusion,
age-key governance, timer activation and measured live RPO/RTO.
