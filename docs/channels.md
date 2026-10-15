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

### Call transcript

`platform/channels/call_transcript.py` records a two-way transcript of each voice call: ordered turns (`caller`, `agent`, and `human` where captured) with offsets, the text actually sent to TTS (interrupted replies stored truncated and marked), a closing outcome (`resolved`, `booked`, `message-taken`, `transferred`, `abandoned`) and a redaction count. Settings are `MODEL_GARDEN_TRANSCRIPT*`:

- A spoken disclosure plays first (`MODEL_GARDEN_TRANSCRIPT_DISCLOSURE`, default notice provided). Setting it to `off` makes the worker refuse to start unless `MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER` names the accountable owner, who is then recorded on every transcript. If the notice cannot be played, the call's transcript is discarded.
- Payment card numbers (Luhn-checked) and client patterns (`MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS`, a JSON array of regular expressions) are redacted before storage and recorded as counts.
- Records go to `MODEL_GARDEN_TRANSCRIPT_PATH` with mode 0600 in a 0700 directory and the same bounded retention as fallback messages (`MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS`, default 7 days; `0` stores nothing). A write failure is logged by error type only, noted as `TranscriptWriteFailed` in the lifecycle log, and never interrupts the call.

Limits today: the model-produced summary, the ledger `requestId`s of the call's governed Tools and STT confidence are not populated by the worker (the record format and `reference` support them); filing into the CRM is an operator-run step, not automatic (see below); speech after a transfer is not captured; the LiveKit event wiring is unit-checked statically and has no live-call proof.

### Filing finished conversations

`python3 scripts/client-operator.py file-calls --project-name <env>` (run on the client host) reads the voice worker's stored transcripts and files each call through the governed `crm.log-interaction` Tool of whatever CRM is bound (`platform/channels/conversation_filing.py`, executed in the governed-tools container by `scripts/runtime-file-calls.py`). It is idempotent on the `transcript:<callHash>` reference, so re-running after a retry or restart adds nothing, and a call a person has already assigned is not filed again. A caller's number is a transport fact, so every call goes to the `unmatched` queue for a person to assign (`client-operator.py contacts review|assign`); the summary holds the outcome and size of the call (or the model summary when the record has one), never the transcript text, and the ledger holds no personal data. Filings are ledgered as `runtime:conversation-filing`. Nothing runs it automatically yet: schedule it or run it after calls. With no CRM bound the command fails closed rather than filing anywhere else, and the transcript stays in the worker's store, visible to the operator.

The same module can file an email once an adapter exists: a sender verified by the receiving server (below) with exactly one matching contact gets the interaction; no contact, several contacts or an unverified sender use the `unmatched` queue; a contact is created for a verified sender only when the deployment enables it *and* `crm.create` actually executes (the reference policy gates `*.create`, so a client must narrow that gate and sign an `allow` rule first). No email handler calls this yet.

## Email

`platform/channels/email_channel.py` holds the parts of the email surface that decide who is writing and whether an approval is genuine; it opens no network connection, and no mail provider, per-client inbound address or hosted web chat is built (#84, #109 remain open).

- **Sender verification.** A sender is verified only by the receiving server's own `Authentication-Results` header: it must carry the deployment's trusted `authserv-id`, there must be exactly one such header, and it must say `dmarc=pass` for the same domain as the single `From` address. Headers from any other server, a missing or ambiguous result, a second `From`, or a DMARC pass for another domain all yield an `unverified:` sender that can never match a directory entry, so it is public and read-only. The mail server must remove inbound headers that already carry its own `authserv-id`. Mapped senders are listed in `channels.directory.email` keyed by lowercase address. Nothing in the message text changes identity.
- **Threading.** `reply_headers` keeps `In-Reply-To`, `References` and `Re:` so replies stay in the sender's thread and strips header injection.
- **Approval links.** `ApprovalLinks` issues an HMAC-signed, expiring token bound to one `requestId`, one decision and one approver address; spending either the approve or the reject token spends both for that request and approver (a private marker file per pair). It proves control of the approver's mailbox only: the approver must still be a mapped `approver` in the directory. Mail scanners fetch links, so the page behind one must require a POST confirmation that submits the token; never act on a GET. The signing key (at least 32 bytes) is supplied by the deployment.

## Status

The contract, directory and governance wiring are implemented and tested credential-free. Slack, Teams, an email provider adapter and web chat are separate stories and must implement this contract rather than bypass it.
