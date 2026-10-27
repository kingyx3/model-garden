# Emergency pause: customer holding and human intake

A mapped client administrator can pause or resume a coworker through signed direct chat or the operator CLI (issue #232). The pause state is durable on the client governance volume; governed material Tools and participating worker jobs share a host-local effect lease. This is **not** remote-provider cancellation, a cloud-wide fence or a completed live incident drill.

## Inbound conversational messages while paused

For a fresh, verified inbound Slack, Teams or private web-chat conversation message, the gateway suppresses the `chat.message` trigger. It does **not** queue an automatic coworker task to run after resume. Instead it creates a local Tier 0 ticket with project `pause-hold` and an idempotent digest of channel/conversation/message identifiers. The ticket has the channel name and creation time but **no message body, sender, address or raw channel identifier**. The original channel remains the source of truth. A signed duplicate does not create another ticket. These safety-intake tickets are deliberately excluded from `ticket.created` worker triggers.

The default holding response says the request is in the private staff ticket inbox **only after** durable ticket creation and a local audit receipt. If intake fails, the reply explicitly says it could not be queued and asks the sender to contact the business. If the ticket was committed but its audit receipt failed, the reply describes that uncertainty. A configured `pausedMessage` can replace the successful default; operators must not promise notification or delivery they have not verified.

Mapped approvers/admins can inspect counts with `inbox` in a direct chat; staff can inspect `tickets` and review the original conversation in its source channel. The local ticket is a review pointer, **not** proof a human received a notification or completed the request. Staff should triage and record completion after authorized resume. No model execution, external message, provider action or approval is performed by the intake path. Ordinary coworker Tool writes remain paused.

## Nonessential scheduled work while paused

The worker holds workspace schedule reconciliation, approved Studio schedule edits and the non-urgent weekly owner digest behind the same host-local pause lease as task execution. A startup tick observed during a pause **does not** consume the one-time schedule reconciliation: it retries on the next tick after authorized resume. The weekly digest neither queues a notice nor advances its sent marker while paused; it may run after resume if still inside its configured grace window. Operator safety alerts, task-completion notices, monitoring, retention and audit export continue while paused; they are not customer-facing autonomous actions.

These are local cooperative fences, not proof of cloud-wide pause or cancellation of an in-flight external provider request. The regression tests cover paused startup, deferred digest and schedule edits, but an authorized tenant-live drill is still required for issue #232.

## Operational limits

The built-in ticket store is private and bounded (5,000 total tickets), and its current retention/cleanup remains an operator responsibility under the client's data policy. A full or unreadable store fails visibly rather than silently claiming handoff. The local audit receipt is not an independently immutable export. A live acceptance drill must still prove channel authentication, receipt, staff response, restart, in-flight provider behavior, cross-tenant/coworker isolation and authorized owner signoff before #232 can close.
