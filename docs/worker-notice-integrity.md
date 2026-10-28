# Worker owner-notice queue integrity (#231, #109)

The runtime worker persists mailbox handoffs, task notices, spend-cap warnings,
knowledge review reminders and persistent-failure alerts in the client-local
`worker/worker-notices.json` queue. This is an operational safety store, not
proof that a named person received a provider message.

## Storage and upgrade contract

- The `worker/` directory must be owned by the worker's effective UID, be a
  real directory (not a symlink), and have no group/other permissions (0700).
- Existing `worker-notices.json` and `.worker-notices.lock` must be private
  regular files (0600), owned by that UID, with one hard link. A linked,
  exposed, malformed or oversized file is refused; it is **never** interpreted
  as an empty queue. The existing lock serializes concurrent workers.
- New state is published using an exclusive unpredictable temporary file,
  file and directory fsync, and atomic replacement. Predictable
  `worker-notices.tmp` paths are no longer used.
- Before upgrading an older deployment, stop the worker, preserve the
  queue and its backup, verify the owner and contents, and have an authorized
  operator correct unsafe file permissions/ownership. **Do not delete or
  replace a suspect queue with an empty one**: doing so can discard an
  undelivered safety alert. Recover it from a verified tenant-scoped backup
  or escalate for independent reconciliation. Restart only after the queue
  and named-person delivery state have been reviewed.
- The queue remains a host-local at-least-once notification mechanism.
  It does not fence remote workers, independently attest owner identity,
  prove external Slack/email delivery, or replace a client-approved
  alert failure/recovery drill. A privileged host administrator can still
  destroy local state; use the separately governed backup/restore flow.

The regression suite is `tests/test_worker_notice_integrity.py`. It uses
synthetic local files and no provider credentials or customer data. Issue
#231 stays open until deployed failure/recovery and delivered alert receipts
are retained; #109 stays open until a real inbound mail and human delivery
run succeeds.
