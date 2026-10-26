# Tier 0 exact-action decisions in private chat

A client without Slack/Teams approval buttons can use the existing signed-in web chat
(or an authenticated direct Slack/Teams conversation) to decide a **pending** governed
Tool approval. The operator must first review the exact action, arguments and policy
using the private client-host approval view. The chat `inbox` intentionally shows
only the Tool name and full 64-character request digest, never sensitive arguments.

1. Sign in as a mapped **approver** in the client/environment's channel directory.
2. In a direct conversation, send `inbox` and locate the pending approval.
3. Inspect the exact action through the private operator approval view; do not
   approve from a Tool name alone.
4. Send `approve <full-lowercase-64-hex-request-id>` or
   `reject <full-lowercase-64-hex-request-id>` in the same verified direct chat.
5. The decision is persisted in the existing approval store and audit ledger.
   It is **not execution**. The coworker must retry the exact governed action;
   expiry, pause, principal/policy and all other runtime checks still apply.

Unmapped senders, non-approver staff and shared channels cannot decide. The first durable decision for an exact request ID wins across web chat, Slack/Teams
buttons, email confirmation and operator CLI. Later approve/reject attempts cannot
reverse it, even from another authorized approver or after a process restart.
Concurrent opposite decisions have one winner. Decided requests disappear from the
actionable inbox; the client host retains the private decision receipt until its
normal TTL sweep. If a fresh request reuses the same ID while its previous decision
still exists, it must wait for the existing decision to expire and be swept;
never delete a live decision to change an answer.

Unknown,
expired, malformed and truncated references do not produce decisions. No external
approval service, production deployment, notification-delivery proof or client
sign-off is implied by the credential-free regression tests. The operator CLI and
existing signed provider approval buttons remain supported.
