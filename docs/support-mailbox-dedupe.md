# Support-mailbox handled-message witness (v0.8.246)

The built-in support mailbox records SHA-256-derived Message-ID digests under its
private client data volume in support-mailbox-handled.json. The witness is
**deduplication state**, not an authorization, delivery receipt or independent
provider acknowledgement. The runtime's spool remains the durable intake queue.

The handler now refuses to process a customer message if the existing witness is
malformed, oversized, linked, foreign-owned or group/world-accessible. The
witness directory and advisory lock must also be private and unlinked. Updates
use a unique exclusive temporary file, fsync the new content, atomically replace
the witness and fsync its directory. A preplanted predictable .tmp path is
never opened or overwritten. After the first successful handled-message
write, the existing persistent `.support-mailbox.lock` also witnesses
initialization. Missing history with an initialized lock now fails closed
before classification, drafting or sending; an orphaned lock after a crash
also requires operator reconciliation. A genuinely new volume with neither
file is initially empty. Valid legacy history without the lock remains
readable and acquires the lock on the next mark. Host-local advisory locking
does **not** protect against privileged writers deleting both files,
cross-host races, or the existing crash window between a provider effect and
its handled marker. This is not exactly-once provider delivery.

**Operator recovery:** A failure must leave the inbound spool item retryable
or quarantined, not silently start a fresh dedupe history. Stop the affected
mail worker; preserve the original volume and Action Ledger privately; check
tenant identity, owner/mode, linked files, index validity and provider-side
draft/send receipts. Restore the last independently verified witness and
reconcile any messages processed after its snapshot before resuming. Do not
blindly delete the file, chmod an untrusted link, reset the history or mark
mail delivered from a local test. Record client approval and an audit receipt
for any repair. Do not put customer addresses, message bodies or credentials
in CI or incident logs.

Credential-free regressions live in
tests/test_support_message_log_integrity.py; they prove fail-closed local
handling and atomic-file boundaries, **not** a real inbound MX, provider draft,
owner approval, delivered message or end-to-end Scenario A/B acceptance.
Remaining live gates are tracked in #109, #77 and #117.
