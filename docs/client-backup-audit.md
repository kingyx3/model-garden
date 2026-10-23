# Read-only recurring backup slot audit (#230)

This **offline-testable, operator-run** tool inspects the approved UTC recurrence
policy and private per-slot claim/outcome receipts written by
[client-backup-schedule.py](client-backup-schedule.md). It **never** starts a
backup, retries an attempt, accesses GCS, delivers an alert, changes a timer,
or claims a verified restore. Its JSON output is an input for an independently
approved monitoring and alert-delivery process, not evidence of notification.

## Invocation

Run under the dedicated host UID that owns the policy and claims. The policy
directory and claims directory must be 0700; the policy, claim and receipt
files must be owner-private 0600 regular files. Parent paths must be trusted.
Choose an explicit --since-utc no earlier than the approved policy start,
no later than now, and within the last 31 days. This bounds historical
inspection; it is **not** authorization to backfill or retry missed slots.

~~~bash
python3 scripts/client-backup-audit.py \
  --schedule /private/acme/policy/schedule.json \
  --claims /private/acme/claims \
  --project acme-dev --client-slug acme --environment dev \
  --gcp-project-id sample-project-123 \
  --recipient "$APPROVED_PUBLIC_AGE_RECIPIENT" \
  --confirm-project acme-dev \
  --since-utc 2026-10-23T00:00:00Z
~~~

## Optional strict exit for an external host supervisor

Add `--strict-slots` to the same command only when an operator-managed
monitor needs a machine-detectable result. The audit remains read-only and
prints its ordinary redacted JSON report in all cases:

- Exit **0**: all *due* slots in the bounded interval have a valid
  `uploaded-not-recovery-proven` receipt. This says nothing about GCS
  read-back, restoration, RPO/RTO or notification delivery.
- Exit **1**: any due slot is `missed`, `unknown` or
  `failed-not-recovery-proven`.
- Exit **3**: no slots are yet due. Treat as **indeterminate**, not success.
- Exit **2**: audit inputs or permissions were refused, and output remains
  redacted as `backup.schedule.audit-refused`.

Without the opt-in flag, all valid audits continue to exit 0 even if their
JSON contains missed slots; existing consumers should inspect the JSON.
Configure the external supervisor's alert and escalation behavior explicitly,
including how it distinguishes nonzero exit classes and suppresses repeated
notifications. A systemd `OnFailure=` hook or provider-native alert channel
is possible only after independent operator authorization and testing. A
successful process exit by itself is **not** a recovery or delivery guarantee.

The tool rechecks the policy's exact client/environment/GCP project/public
age-recipient binding, weekdays and UTC slot time. It reports each slot in
the requested interval only **after** the five-minute dispatch grace period:

- **missed**: no claim and no result after the grace period.
- **unknown**: an attempt without a result, an orphan result, unsafe or
  malformed files, a mismatched tenant/slot, or an unrecognized status.
- **failed-not-recovery-proven**: a valid failed-upload receipt.
- **uploaded-not-recovery-proven**: a valid upload receipt, **not** a remote
  read-back, restore, or RPO/RTO proof.

No due slots produces an empty slots array, **not** a success signal.
Every output explicitly sets recoveryProven=false and
notificationsDelivered=false. An invalid policy, mismatched identity,
insecure claims directory or unbounded inspection fails closed with a
redacted backup.schedule.audit-refused event.

**Operations:** Invoke this read-only audit from an independently reviewed
host monitor and route its structured events to an explicitly approved,
tenant-isolated channel only after delivery, retry and deduplication controls
are implemented and tested. Do not treat repeated audit results as sent
alerts. Do not delete claim/receipt files to clear an incident. Investigate
unknown/missed slots and obtain a new client-approved slot before attempting
another backup. Cross-host restart exclusion, authorized timer activation,
age-key governance, real notification delivery and live recovery drills
remain separate release gates.
