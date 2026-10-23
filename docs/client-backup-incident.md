# Tenant-isolated backup incident transitions (#230)

The opt-in `scripts/client-backup-incident.py` builds on the existing
[read-only slot audit](client-backup-audit.md) to produce **durable, deduplicated
incident-open and incident-recovered signals** for an independently approved
host supervisor. It does not install a timer, contact a notification provider,
or prove delivery, GCS read-back, or successful restoration.

## Run only after an authorized monitoring plan

Use the same dedicated host UID as the private schedule and claim receipts.
Create a **separate tenant/environment-specific** 0700 state directory owned
by that UID. Keep its parents trusted. State and lock files are owner-private
0600 regular files; unsafe links, modes, mismatched bindings, corrupted state
and concurrent invocations fail closed.

~~~bash
python3 scripts/client-backup-incident.py \
  --schedule /private/acme/policy/schedule.json \
  --claims /private/acme/claims \
  --state-dir /private/acme/incident-state \
  --project acme-dev --client-slug acme --environment dev \
  --gcp-project-id sample-project-123 \
  --recipient "$APPROVED_PUBLIC_AGE_RECIPIENT" \
  --confirm-project acme-dev \
  --since-utc 2026-10-23T00:00:00Z
~~~

The monitor reuses all existing recurrence, tenant/cloud/key, private-file,
bounded-lookback and five-minute grace checks. Use a continuously overlapping
inspection interval: `--since-utc` must be **at or before** the last inspected
slot on subsequent runs. Gaps are refused rather than silently marking missed
slots healthy. The first invocation establishes a baseline. If its due slots
are unhealthy, it opens an incident immediately.

## Events, idempotence and limitations

- `backup.incident.opened`: first missed, unknown, or failed due slot since
  the last observed slot (or newly detected deterioration of that same slot).
- `backup.incident.unchanged`: repeated unhealthy polls are suppressed;
  repeated healthy polls also produce no new transition.
- `backup.incident.recovered`: a **newer** due slot is uploaded with a valid
  local receipt and no newly missed/failed/unknown slots. Replacing a receipt
  for the same failed slot cannot create a recovery transition.
- `backup.incident.indeterminate`: no due slots; never evidence of recovery.
- `backup.incident.refused`: unsafe input/state or unobserved monitoring gap.

Each JSON event contains only client project, latest UTC slot, fixed severity
and runbook action, incident-active flag and explicit
`recoveryProven=false`, `notificationsDelivered=false`. No secrets, public
age recipient, approval documents or exception messages are emitted. Process
exit: **1** while incident active; **0** for healthy; **3** when no due slots
and no active incident; **2** on refusal. The operator should investigate
claims, upload integrity and independent restore evidence before concluding
that the incident is resolved operationally.

The incident state is atomically replaced and fsynced under a per-project
nonblocking advisory lock. This prevents concurrent duplicate transition
generation on one trusted host, but it is **not** a distributed deduplication
system. A crash between state persistence and stdout consumption can lose a
one-time transition event. Therefore, route the **level signal**
`incidentActive` to an independently authorized provider-native monitor,
and use provider incident-open/close semantics for delivery/retry/dedup.
Do not infer a notification was delivered from this CLI's output. Never
automatically clear or delete state to make an alert disappear.

**Still required:** explicit client-approved notification-channel ownership,
actual provider-native delivery with retry and recovery verification,
cross-host deployment/restart freeze, approved timer activation, age-key
governance, and an authorized live cloud restore drill with measured RPO/RTO.
Issue #230 remains open. This implementation makes no production change.
