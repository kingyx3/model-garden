# Schedule owner privacy in chat

A schedule is owned by a verified principal. `schedules`, `schedule create`,
`trigger create` and `schedule pause|resume|delete` are **private management
commands**. The channel gateway accepts them only in direct conversations
after transport verification and identity mapping.

An allow-listed Slack channel or Teams group may address the coworker, but
a mapped participant's identity does not make that shared audience private.
Shared-channel schedule commands receive only a generic direct-message
instruction; the gateway must not query or mutate the owner's schedule store,
disclose schedule IDs/goals, or send a private listing to the group. Direct
messages still enforce the schedule store's owner-only controls and budget
requirements. Unverified direct senders cannot manage schedules.

The gateway's Slack/Teams direct-conversation classification is transport
specific; a real provider integration still requires approved tenant/channel
configuration and live verification. Credential-free tests establish the
routing privacy contract, **not** provider-delivered confidentiality or a
production scheduling guarantee. See issue #99 for the remaining runtime
acceptance and operational evidence.
