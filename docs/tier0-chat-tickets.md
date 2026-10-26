# Tier 0 ticket triage from verified direct chat

For a client with no external work-item provider, a mapped staff member can manage the
existing built-in `tickets` store from signed web chat or a verified Slack/Teams direct
conversation. This is a **human action**, not a request for the AI to execute a Tool.
No external SaaS connector, provider OAuth credential or special approver role is needed.

1. Sign in through the configured channel as a member mapped to a unique client identity.
2. Send `tickets` (or `/tickets`) to list up to 20 open ticket references,
   projects and statuses. The list deliberately excludes titles, descriptions,
   customer identifiers and comments.
3. Review the ticket's exact content through the private client-host
   `python3 scripts/runtime-tickets.py show <ticket-id> --tenant <client>` view
   before making a decision; the chat inbox alone is not sufficient context.
4. Send `ticket t-0123456789ab in-progress`, `ticket t-0123456789ab waiting`,
   `ticket t-0123456789ab done` or `ticket t-0123456789ab cancelled`.
   An optional short note after the status is stored privately, never echoed in
   the chat reply or copied to the Action Ledger.
5. Confirm the reply and reconcile the `ticket.update` Action Ledger event.
   The event contains the tenant, full ticket reference, status, acting mapped
   identity, `via: chat` and whether a note was supplied; it never includes
   ticket text or note contents.

The channel adapter verifies the sender before routing. Unmapped people, public
senders and group conversations cannot list or change tickets. The gateway uses
its **own compiled client tenant** rather than accepting a tenant in the message,
so a reference from another client cannot be updated. Invalid references, invalid
statuses, missing tickets and already-final `done`/`cancelled` tickets fail
without mutation. The gateway serializes writes with the owner pause lock and
rechecks pause before changing anything; listing remains read-only while paused.
If the store changes but the ledger write fails, the reply explicitly says that
the status changed and requires operator reconciliation instead of claiming that
nothing happened.

**Limits and evidence:** This is a pull-only ticket list, not push notification,
SMTP inbox delivery or client-accepted production evidence. There is no customer
self-service file upload, no live mailbox routing, and no proof that a named
staff member received a notification. The ticket store is a small private host
store, not an independently retained immutable export. Issue #77 remains open
until its full original acceptance and live evidence gates are met.
