# Support-mailbox handled-message witness (v0.8.201)

The built-in support mailbox records SHA-256-derived Message-ID digests under its
private client data volume in support-mailbox-handled.json. The witness is
**deduplication state**, not an authorization, delivery receipt or independent
provider acknowledgement. The runtime's spool remains the durable intake queue.

The handler now refuses to process a customer message if the existing witness is
malformed, oversized, linked, foreign-owned or group/world-accessible. The
witness directory and advisory lock must also be private and unlinked. Updates
use a unique exclusive temporary file, fsync the new content, atomically replace
the witness and fsync its directory. A preplanted predictable .tmp path is
never opened or overwritten. A missing witness in a new client data volume is
still interpreted as an empty history; protect the complete volume and its
backups against deletion. Host-local advisory locking does **not** protect
against privileged writers, cross-host races or a deleted witness and does not
provide exactly-once provider delivery.

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
