# Channel adapters and verified principals

Every text surface (Slack, Microsoft Teams, email, built-in web chat) implements one contract, `platform/channels/text_channel.py`, so the coworker behaves the same everywhere and its authority always comes from a verified person rather than from message text.

## Contract

An adapter is transport-only. It implements `TextChannelAdapter`:

| Method | Responsibility |
| --- | --- |
| `parse_inbound(body, headers)` | Verify the provider's request signature, then return an `InboundMessage`: channel, conversation ID, thread ID, message ID, the verified sender (`ChannelIdentity`), text and attachment references. Raise `ChannelVerificationError` when verification fails. |
| `parse_approval_response(body, headers)` | Return an `ApprovalResponse` (request ID, `approve`/`reject`, verified responder) only for a verified structured interactive payload such as a button or card action. Free text is never an approval. |
| `send_reply(OutboundReply)` | Post a reply into the conversation/thread. |
| `send_receipt(ActionReceipt)` | Tell the thread what a governed Tool call did, with its ledger `requestId`. |
| `send_approval_prompt(ApprovalPrompt)` | Ask for an exact-action decision on one `requestId`. |

Attachments are references (ID, name, media type, size); content is fetched only through governed Tools.

## Principal mapping

`PrincipalDirectory` maps a verified channel user to a client identity from the EnvironmentBinding:

```yaml
channels:
  directory:
    slack:
      T01ABC/U02DEF: {identity: alice@client.example, roles: [approver]}
    teams:
      29:1a2b3c: {identity: bob@client.example}
```

- Keys are scoped by the channel workspace/tenant when the provider has one, so a user ID from another workspace never matches.
- A mapped user becomes a `member` principal with that identity and roles.
- An unmapped user becomes a `public` principal with a pseudonymous `unmapped:<channel>:<digest>` identity. Governance denies public principals every non-`read` Tool (`principal-not-authorized`, ledgered like any other denial) and they can never approve.
- `principal_for()` reads only the verified sender. Text that claims to be another user, an approver, or a system instruction has no effect.

The principal flows into governance as the initiating identity: `initiatingUser`, plus `originatingPrincipal` and `channel` in the bounded governance context, so the `requestId`, approval binding and ledger all name the real person. The governed sidecar takes a `principal_resolver` for this; when a channel gateway supplies one it replaces the static `MODEL_GARDEN_INITIATING_IDENTITY`, which remains the identity for runtime-only calls.

Channel approvals go through `decide_from_channel()`: the responder must be mapped with the `approver` role, and their directory identity, never anything typed in the message, is recorded as the approver.

## Voice (LiveKit)

The LiveKit Receptionist follows the same semantics. A phone caller is an unverified channel user: the SIP participant identity and caller number are transport facts, not a client identity, so a caller maps to a public principal and must never be treated as a client member. Today the voice Receptionist calls governed Tools with the runtime workload identity rather than a channel principal; its only write (`calendar.book`) requires exact-action approval by a human operator, so no caller statement can authorize a write. A deployment that verifies callers (for example a mapped staff line) maps them through the same directory under the `voice` channel.

## Status

The contract, directory and governance wiring are implemented and tested credential-free. Slack, Teams, email and web chat adapters are separate stories and must implement this contract rather than bypass it.
