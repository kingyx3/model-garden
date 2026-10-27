# Optional client backup health level signals in Cloud Logging (#230)

`scripts/client-backup-cloud-log.py` is an **operator-invoked, default-off**
Cloud Logging bridge for the durable backup incident monitor. It writes one
**redacted LEVEL signal on every poll**, including repeated unhealthy polls;
this avoids relying on a transition that could be lost after local persistence.
It is not an alert policy, does not create notification channels, and does not
prove Cloud Monitoring delivery, upload integrity, or successful restoration.

## Authorized use

Require client-approved monitoring and a trusted, separately provisioned
`gcloud` binary on the host. The executing identity must already have narrowly
scoped Cloud Logging write permission in the **same approved client GCP
project**. This change grants no IAM roles and does not install a timer.
Keep the private recurrence/claims/state directories, overlapping UTC lookback,
and trusted host UID rules from [incident monitoring](client-backup-incident.md).
The explicit GCP confirmation must match the policy-bound project; never run
this with a broad operator identity or in a shared cross-tenant project.

~~~bash
python3 scripts/client-backup-cloud-log.py \
  --schedule /private/acme/policy/schedule.json \
  --claims /private/acme/claims \
  --state-dir /private/acme/incident-state \
  --project acme-dev --client-slug acme --environment dev \
  --gcp-project-id sample-project-123 \
  --confirm-gcp-project-id sample-project-123 \
  --recipient "$APPROVED_PUBLIC_AGE_RECIPIENT" \
  --confirm-project acme-dev --since-utc 2026-10-23T00:00:00Z
~~~

The script runs the existing strict audit/incident monitor first, then uses
`gcloud logging write model-garden-backup-health` with a **20-second timeout**,
explicit `--project`, structured JSON payload, and suppressed subprocess
stdout/stderr. It uses no shell, arbitrary log name, or forwarded exception
text. Payload fields are allowlisted: `schemaVersion`, `component`,
`clientProject`, `health`, `incidentEvent`, optional `slotUTC`, and explicit
`recoveryProven=false`, `notificationsDelivered=false`. No public age key,
credentials, approval text, customer payload or change reference is logged.

`UNHEALTHY` emits ERROR, `HEALTHY` emits INFO, and no due slot with no active
incident emits **UNKNOWN/WARNING**, never HEALTHY. An indeterminate poll while
an incident remains active is UNHEALTHY. Repeated polls deliberately emit the
same level; downstream Cloud Monitoring must use a client-scoped, deduplicated
incident policy rather than treating each log line as a new notification.

Exit 0 means a healthy level was **accepted by the local gcloud command**,
1 means unhealthy, 3 means indeterminate, and 2 means monitoring refusal,
invalid binding, CLI timeout or rejected write. These are **not** proof of
Cloud Logging ingestion, alert delivery, or recovery. On failure, the next
approved poll can re-emit the current level; never clear incident state or
backup claims to force a notification or retry. An authorized operator must
configure and live-test an existing project-scoped Cloud Monitoring alert
policy/channel separately; alerting is **not** enabled by this code.

**Still open:** verified provider-native incident delivery and recovery
notification, enforceable cross-host deployment/restart freeze, age-key
rotation governance, authorized timer activation, and a live cloud recovery
drill with measured RPO/RTO. Keep issue #230 open.

## Durable recovery-notice replay (v0.8.144)

The incident monitor now fsyncs a private `pendingRecoverySlot` with each
UNHEALTHY → HEALTHY transition. If the host crashes or `gcloud logging write`
fails after the local state changed, later healthy polls **replay the pending
recovery event**, not an ordinary healthy level, until a successful local
Cloud Logging command is followed by a separate fsynced acknowledgement.
A newer unhealthy observation cancels the stale pending recovery; an
acknowledgement must match the exact tenant/key/slot and healthy state.
A crash after cloud acceptance but before acknowledgement can duplicate the
recovery log. The existing native alert policy rate limit bounds notifications;
no exactly-once or confirmed notification delivery is claimed. Monitor the
command's exit status and the authorized channel independently. A successful
upload-health transition is **not** successful restoration or measured RPO/RTO.

## Contiguous automatic lookback for an approved host timer (v0.8.176)

An authorized operator-managed timer may replace the manual
`--since-utc 2026-10-23T00:00:00Z` argument with `--auto-since` (exactly
one of these two arguments is required). This does **not** install a timer,
enable a Cloud Monitoring alert, or approve any backup or cloud action.

Automatic mode validates the private recurrence policy, owner-private incident
state, exact client/environment/GCP/age-key binding, and the monitoring
interval **before** invoking the incident monitor or Cloud Logging. On first
use, inspection starts at the recurrence's approved `validFrom`, so missed
earlier slots cannot be hidden by starting from "now." On subsequent polls,
inspection starts at the **last durable observed UTC slot**, overlapping the
previous result across timer restarts and detecting new missed slots. A missing
state is a first-use baseline only; a corrupt/foreign state fails closed.
If a first-use policy is older than 31 days, or a persisted last slot is over
31 days old, the command refuses rather than silently skipping an unobserved
interval. An operator must investigate and preserve evidence before any
authorized monitoring reset. Do **not** delete incident state to manufacture
a healthy level.

Example: use the command above with `--auto-since` in place of `--since-utc`
for an explicitly approved, periodically invoked host service. Use the same
dedicated UID that owns the 0700 schedule/incident directories and 0600
files. Watch nonzero exit statuses independently: exit 2 can mean an unsafe
or unbridgeable monitoring gap, and **no Cloud Logging sample was accepted**.
A working timer, successful local log command or healthy upload level is
neither a delivered alert nor successful restore proof. Continue to require
authorized GCP notification-channel configuration, operator acknowledgement,
cross-host deployment freeze and a measured live recovery drill.

