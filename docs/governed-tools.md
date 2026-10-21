# Governed Tools runtime

Model Garden exposes supported business Tools to Hermes through standard MCP while keeping authorization, connector credentials and audit outside the model process.

## Runtime shape

```text
Hermes / model
  model credential only
  hermes-data only
       |
       | MCP (internal Docker network)
       v
governed-tools sidecar
  selected connector credential only
  governance-data only
       |
       +-> Model Garden allow / approval / deny
       +-> versioned tamper-evident JSONL audit
       +-> approved connector
```

The sidecar is not a control plane and is not exposed on a host port. It is part of the same isolated client Docker deployment and consumes the compiled desired state of one Agent. Several Agents run as one such sidecar each (`scripts/runtime-team.py prepare`, see "Team of Agents" in the contracts), sharing only the ledger; the Docker deployment still builds one. Persistent approval and audit state is mounted only into the governed Tool sidecar; Hermes does not mount that governance volume and therefore cannot create approval evidence by writing directly to the file-backed operator store.

Hermes only receives MCP Tool names corresponding to Tools explicitly selected by that Agent. Provisioning fails closed if a selected Tool has no implemented governed runtime binding.

## Tenant-approved MCP activation (v0.8.4)

The runtime now resolves **only a private tenant-scoped connection ID** rather than a caller-supplied MCP URL or declaration. `McpConnectionStore` under `platform/core/mcp_connections.py` stages a validated HTTPS declaration disabled by default, requires named client-owner consent, operator approval, individually reviewed and schema-pinned operations, a matching portable catalog risk and **an independent per-coworker grant bound to the exact connection ID**, then allows activation. Disabling a connection takes effect without a redeploy; replacement deactivates it and requires a fresh review. Read-only public operator status never includes secret references. The private store contains **only a `secret://` reference**, not its value. Reviews, activation, replacement and disablement expose a non-secret audit hook.

`GovernedToolRuntime.upstream_call(connection_id, upstream_name, arguments)` resolves this state using its compiled Agent identity and tenant manifest, and rechecks status, grant, exact route revision and activation-time tool fingerprints immediately before calling the injected upstream transport. Exact-action approval IDs include a nonsecret digest of tenant, coworker, connection revision, grant ID **and a newly generated activation ID**; an approval from before disabling, reactivating or replacing a connection cannot be replayed. Failures and invalid JSON schema inputs are returned without echoing raw provider error text or confidential arguments. This is the internal governance contract. Subsequent operator-assisted enrollment, sealed-vault storage and pinned upstream MCP/REST transports have now been implemented; it remains **neither a self-service customer connector UI nor evidence of any live source-provider connection** (#87/#158/#159/#86).

## Isolated coworker grant provisioning (v0.8.11)

The operator can now create, rotate, mark failed/recovered and revoke **one coworker's provider grant** without editing the private registry. This is a credential-reference and authorization **record** only: it does not create an OAuth consent, fetch or store a token, connect to an MCP server, or mark a provider live. Provision each coworker's own upstream identity and secret in the approved tenant vault first. Run inside the private governed-tools operator environment, never from Hermes/the model:

```bash
python3 scripts/runtime-connection.py /opt/data/.modelgarden grants create \
  --tenant acme-prod --agent receptionist --family crm --provider hubspot \
  --connection-id crm-main --isolation own-identity \
  --acting-identity receptionist@client.example \
  --secret-ref secret://acme/prod/receptionist-v1 \
  --scope crm.read --scope crm.create --owner owner@client.example \
  --actor operator@client.example

python3 scripts/runtime-connection.py /opt/data/.modelgarden grants rotate g-0123456789ab \
  --tenant acme-prod --secret-ref secret://acme/prod/receptionist-v2 --actor operator@client.example

python3 scripts/runtime-connection.py /opt/data/.modelgarden grants health g-0123456789ab failed \
  --tenant acme-prod --actor operator@client.example
```

Use `--manifest <path>` with `--tenant` to cross-check the intended environment: mismatches are refused. Creation accepts only catalogued portable Tools in the named family, only isolated `own-identity` or `isolated-grant` access (never `shared-login`), and returns a redacted public grant view. Changes are audit-logged without raw secret references or values; a `secret://` reference is **not** itself a secret. After revocation the old secret reference cannot be reused; provision a new credential reference and repeat connection review/activation as applicable. Separate client-owner consent and independently reviewed upstream schema remain mandatory. This provisioning command alone does not make external integrations live.


## MCP connection lifecycle hardening (v0.8.9)

- **Revocation and review are fenced:** an activation can no longer commit if an operator disabled or replaced a connection during schema/grant review, even if the manifest content stayed identical. Final activation checks fresh schema approval and the independently scoped coworker grant under a state transition lock. Concurrent activations cannot silently replace each other's activation identity. Disabling discards prior activation fingerprints/identity and denies subsequent execution until owner consent and review are repeated.
- **Separation of duties:** the consenting client owner and activating operator must be different named principals. A reviewer cannot treat their own consent as an independent decision.
- **Operator-only lifecycle command:** `python3 scripts/runtime-mcp-connections.py /opt/data/.modelgarden --tenant acme-prod status --agent receptionist` lists redacted connections. The `stage --agent receptionist --id crm-main --file /private/reviewed-mcp.json --actor operator@client.example` command saves only a vetted HTTPS `secret://`-reference manifest as **inactive**; `--replace` deactivates existing state. `disable --agent receptionist --id crm-main --actor owner@client.example` takes away that coworker's connection on the next call. All changes are auditable operator actions, and the CLI never accepts credentials or connects to an MCP server.
- **Still not a provider go-live:** the operator CLI deliberately has no `activate` command accepting an untrusted static tool listing as proof. Runtime activation still needs independently verified server schema and a real, pinned-address, reviewed external transport/secret resolver plus client-tenant evidence. External providers stay planned.

## Governed upstream MCP execution seam (v0.8.3)

The internal governed runtime now has a credential-free, injectable MCP execution seam (`platform/core/mcp_executor.py` and `GovernedToolRuntime.upstream_call`). Before an outbound request, it requires a selected portable Tool with catalog-matching risk; an available and approved Tier 2 binding; tenant- and coworker-specific provider authorization with independent identity, correct Tool grant, scoped secret reference and usable health; and a currently approved upstream tool schema. Requests execute through the **existing** allow/approval/deny and exact-action approval/audit boundary, with connection, grant, acting identity and upstream operation attribution. A revoked grant, missing permission, changed/poisoned tool schema, private or rebinding DNS answer, unsafe input schema, or missing credential fails closed. Tests inject two business operations on an upstream transport fixture.

**Concrete transport (v0.8.20).** `platform/runtime/upstream_http.py` is that transport, installed only when the binding sets `upstream.mcp.enabled: true` (`MODEL_GARDEN_UPSTREAM_MCP=enabled` on the governed sidecar). It speaks only the streamable-HTTP MCP methods `initialize`, `tools/list` (bounded paging) and `tools/call` to the one declared server, dials the IP the endpoint guard pinned while verifying the certificate for the declared host name, never follows a redirect, bounds request and response sizes, and keeps the bearer credential in the `Authorization` header only (errors carry an HTTP status or JSON-RPC code, never a URL path, header or body). Credentials are resolved from `secret://` references to environment variables injected into the sidecar alone (`binding.upstream.mcp.secret_refs`); the Hermes process never receives them. An activated connection's approved operations are registered with the model under their portable Tool name (for example `crm_create`), replacing the Tier 0 built-in of the same name; the model never sees the endpoint, the upstream tool name or the credential, and every call still runs allow/approval/deny, exact-action approval, per-coworker grant resolution and the ledger. A connection activated or disabled while the sidecar runs is picked up on its next restart. The tool takes an `arguments` object that must match the operation's reviewed input schema, so a Skill written for the Tier 0 argument shape needs the same operation contract on both routes (#117).

Enabling the transport approves nothing: activation still needs the owner's consent, a separate operator, reviewed fingerprints and the coworker's own grant. **No provider is live:** real owner consent and secret custody, constrained egress and tenant proof (#87/#158/#86/#129/#66) remain, and the canonical registry keeps SaaS providers planned. No arbitrary raw MCP call is registered for the model.

## Upstream-only SaaS Tools (v0.8.0)

Model Garden no longer ships bespoke Google/Microsoft Calendar, Gmail/Outlook, HubSpot/Salesforce or Drive API clients. A generic OAuth-consent, vault and refresh path exists for independently reviewed upstream connections, but is not an in-house vendor API client and has no real-tenant provider proof. These SaaS systems remain **planned upstream routes**, not available live connections. The reference Calendar Skill and its portable `calendar.availability` and `calendar.book` contracts remain valid, but cannot be activated until an approved external MCP/managed integration demonstrates those exact operations.

Use maintained vendor MCP servers where available; evaluate Activepieces and other maintained open-source/managed projects as alternative backends. The [integration registry](../contracts/v1/integrations.json) states which providers are planned versus evidenced. `mcp_passthrough.py` defines operation/schema approval; `upstream_http.py`, the governed sidecar, `custom_api.py` and the owner enrollment/OAuth mechanisms implement credential-free-tested execution and custody boundaries. **Still outstanding:** independent live source-provider identity/consent, real-tenant proof and customer self-service (#157–#159, #87, #129). Never connect upstream servers directly to Hermes or bypass governance.

**Migration safety:** v0.8.0 is a breaking change for previous native Calendar deployments. Keep an earlier pinned version if a client depends on that integration; upgrade when the replacement is tested and individually authorized. A legacy provider binding or a cached OAuth credential does not establish readiness. The compiler, profile provisioning and Docker deployment fail closed on unsupported Calendar Tools. No Calendar credentials are injected into the sidecar or voice worker in this release.

## Capability manifest

Deployment writes a non-secret capability manifest per environment (`profile/.modelgarden/capability-manifest.json` in the Docker bundle). For every catalog family it states the status (`available`, `not-granted`, `unbound`, or `unavailable` once a connection has failed or been revoked), provider, tier, granted scopes, connection owner, health and fallback. It carries no token references, calendar IDs, mailboxes or tenant identifiers beyond the client and environment name.

The governed sidecar exposes it to the coworker as the read-only `platform.capabilities` MCP Tool (`platform_capabilities`), so "what can you do here?" is answered from what is actually connected. It is a platform built-in rather than a business Tool, so it bypasses Tool selection and is not ledgered; Hermes receives it whenever the governed sidecar is present. The sidecar is present when the Agent selects a supported Tier 0 Tool (`kb.search`, `crm.read`, `crm.create`, `crm.log-interaction`); a deployment with none of them has no sidecar.

Connection health is live, not baked into the deployment. The sidecar records `healthy` after a successful connector call, `failed` after a provider 401/403 and `degraded` after other provider errors. Operators can record a revocation or recovery without redeploying Agents:

```bash
python3 scripts/client-operator.py connection calendar revoked \
  --project-name acme-prod \
  --actor operator@example.com --reason "consent withdrawn"
python3 scripts/client-operator.py connections --project-name acme-prod
```

Every health change is a chained ledger event naming who made it: an operator change writes an `operator-action` (`connection.set`) with the family, new health and reason, and the sidecar's own transitions (first healthy use, degraded, failed) write `connection.health` under `runtime:governed-tools`; unchanged health writes nothing. Events never contain a credential, scope or account. `client-operator.py inbox` lists failed, revoked and degraded connections (with the reason code and the command to record a recovery) next to approvals and hand-offs, so a withdrawn or expired token is noticed in one place, but nothing notifies the connection's owner.

**Offboarding.** `client-operator.py offboard --project-name acme-prod --actor <person> --reason "contract ended"` revokes every bound external (tier 1 and above) connection in the capability manifest at the governed boundary and writes one `environment.offboard` ledger event, then prints the steps only a person can do: revoke each grant at the provider, delete the environment's secrets from the secret store, export and verify the ledger, decide what to erase under the client's retention rules (contact log, transcripts, messages, hand-offs, approvals) and tear the stack down. Revoking here only stops the coworker using a grant; it does not revoke it at the provider or delete anything, and Tier 0 stores are local data covered by the manual steps.

**Per-coworker offboarding.** When the environment has a per-coworker grant registry (`provider-grants/<client>-<environment>.grants.json`, see "Per-coworker provider grants" in the contracts), `offboard` also revokes every grant in it, each as its own ledger event (`grant.revoke`, secret reference as a digest only), and lists the **secret references** of the revoked grants as `secretRefsToDelete` so each can be removed from the secret store. `client-operator.py offboard --project-name acme-prod --agent receptionist --actor <person>` offboards **one coworker**: its grants are revoked, one `coworker.offboard` event is written, the environment-level connection health is left alone and every other coworker keeps working; the next `platform.capabilities` read shows that coworker's connected Skills as `grant-unavailable`. Repeating an offboarding changes and ledgers nothing. `client-operator.py grants list --project-name acme-prod [--agent receptionist]` shows grants (never a secret reference) and `grants revoke g-... --actor <person>` revokes one. As above, this stops the coworker using the grant; revoking at the provider and deleting the secret remain manual steps.

The next `platform.capabilities` read reports the change. **Runtime enforcement:** a connection recorded as `failed` or `revoked` also denies subsequent governed calls for that capability (once an upstream executor is integrated), including those with an earlier approval, and records a redacted denial in the chained action ledger. Only an authorized operator can mark a reauthorized connection healthy; in-flight API responses cannot clear a revocation. Marking a connection `revoked` is a **local execution kill switch**, not a substitute for revoking the underlying client OAuth grant/provider credential or clearing any pending human approvals. The operator must also revoke the upstream grant and rotate/delete the protected secret under the client offboarding runbook. Do not equate a local status update with external revocation. The Receptionist eval suite checks that an unbound capability is reported with its human hand-off fallback and that the instructions tell the coworker to decline and offer that fallback instead of claiming success.

## Exact-action approval

An approval-required Tool call returns `pending-approval` and persists the exact request material on the sidecar-only `governance-data` volume. The request ID binds Agent, Tool, exact arguments, initiating identity and bounded first-party governance context.

List pending requests from the Docker host:

```bash
python3 scripts/client-operator.py approvals \
  --project-name acme-prod
```

Approve one exact request:

```bash
python3 scripts/client-operator.py approve <request-id> \
  --project-name acme-prod \
  --approver operator@example.com
```

Or reject it:

```bash
python3 scripts/client-operator.py reject <request-id> \
  --project-name acme-prod \
  --approver operator@example.com
```

Whether a call needs approval is the stricter of the Agent switch, the Tool's `approvalRequired` and the client's `Policy` rules; see [Client approval policy](contracts.md#client-approval-policy) for precedence, the public-principal floors and owner sign-off. Channel approvals already require a mapped `approver`, so a public caller cannot approve their own request.

Changing the Tool arguments or bounded governance context creates a different request ID, so an approval cannot authorize a materially different action. Approval decisions are also single-use: once an approved or rejected decision is applied, it is consumed. A later identical material action must pause for a fresh human decision rather than replaying an earlier approval.

**Approvals expire.** An approval is for now. A pending request records when it was asked (`requestedAt`) and a decision when it was made (`decidedAt`); both stop being valid after `MODEL_GARDEN_APPROVAL_TTL_HOURS` (a whole number of hours from 1 to 720, default 24; set it per client with `approvals.ttl_hours` in the EnvironmentBinding (a whole number from 1 to 720; the deployer rejects anything else or an unknown key under `approvals`) and the generated compose passes it to the sidecar, worker and gateway containers and never to Hermes; a bad value stops the sidecar starting rather than silently never expiring). An expired request is not announced to approvers, is left out of the inbox, and a late `approve` is refused ("ask for it again"). An expired *decision* is never used: if the coworker repeats the same action after the window, the sidecar discards the old decision, writes an `approval.expired` ledger event (request id, kind and age only) and returns `pending-approval` again, so a "yes" given on Monday cannot authorise an identical action on Friday. Asking again while a request is still live does not extend it; asking after it expired starts a new window. The worker's hourly retention job removes expired requests and decisions and ledgers each one; when a *request* expired undecided (so an action will now never run) it also tells `escalateTo` (else the mailbox owner) once through their own channel, naming only the Tool and a short reference, never the arguments or the reason. Records written before this release have no timestamp and are aged by their file time. This changes only how long a human decision stays usable; it does not make approvals live-proven (see [live proof](live-proof.md)).

This file-backed approval mechanism is intentionally an MVP operator surface. Do not build an approval portal or workflow engine until repeated client evidence justifies one.

## Human hand-off (Tier 4)

When an action cannot run here, the coworker prepares it for a named person instead of failing or pretending. Two triggers:

- **Unbound or unavailable capability.** The platform Tool `handoff_request` takes the exact portable Tool name and arguments of a **write** Tool the Agent selected. It is accepted only when `platform_capabilities` shows that family as `unbound` or `unavailable` with fallback `human-handoff`; otherwise the call is refused ("call the Tool directly"). Reads cannot be handed off. A revoked or failed connection still denies direct Tool calls exactly as before; `handoff_request` is the explicit alternative.
- **Policy `human-only`.** A matching `human-only` rule (which must name an `assignee`, optionally `deadlineHours`) forbids automation: the direct Tool call returns `handed-off` and nothing executes, and no approval can change that. Deny rules still win; public principals cannot reach a write Tool this way.

The hand-off holds the same exact request material that approvals bind to (so it is as sensitive as a pending approval), is stored on the sidecar-only volume at `/opt/data/.modelgarden/handoffs`, and is created idempotently per exact request. The assignee is `MODEL_GARDEN_HANDOFF_ASSIGNEE` or the policy rule's `assignee`; with neither, the call fails closed.

```bash
python3 scripts/client-operator.py handoffs --project-name acme-prod
python3 scripts/client-operator.py handoff-done <id> --project-name acme-prod --by jordan@client.example
python3 scripts/client-operator.py handoff-decline <id> --project-name acme-prod --by jordan@client.example --note "not our policy"
```

Creating and closing a hand-off each write a chained `handoff` ledger event with `tier: 4` and `provider: human`; closing records the person's identity and `late` when the deadline passed. As with approvals, that identity is what the operator command asserts (`--by`); the platform does not verify that the person is the assignee or independently confirm that they performed the action. The coworker learns the outcome only through `handoff_status`, whose message says the action has **not** been done while pending, names the person who confirmed it when done (and that the coworker did not do it itself), and says it was **not** done when declined. The receptionist instructions and the `unavailable-action-handoff` eval require that wording.

`python3 scripts/client-operator.py inbox --project-name acme-prod` (also `--json`) is the one view of everything waiting for a person: pending approvals (oldest first), open hand-offs (overdue first, with assignee and deadline), customer-email CRM filings that blocked and need a person (a count and the oldest age; `client-operator.py crm-sync list` shows why and `crm-sync retry` repairs) and counts of contact records to review. It names the Tool, assignee, deadline and record ID and the command that shows the exact content, but never action arguments, summaries or personal data, so it can be pasted into a chat. It is a read-only view over the approval, hand-off and contact stores (`platform/core/inbox.py`), not a `tickets.*` provider, and nothing notifies anyone: someone has to look at it.

**Notices.** `client-operator.py handoffs-due [--escalate-to <owner>]` prints the notices that should reach a person now: `assigned` (they have not been told it exists), one `reminder` shortly before the deadline (a quarter of the window, between 30 minutes and 2 hours) and one `escalation` once it is overdue, to the named escalation contact or else the assignee again. A notice carries the Tool, deadline and a short reference, never the action's arguments or personal data, so any channel can carry it; the person opens the exact content with `handoffs`. Nothing sends them: a deployment's channel transport (a mailer, the Slack or web chat adapter) delivers each notice and then records it with `client-operator.py handoff-notified <id> <kind> --via <channel>`, which ledgers an operator action and stops it being offered again. A notice that was not recorded is offered again, so delivery is at-least-once. No transport is shipped and no timer runs `handoffs-due`: until a deployment wires both, the built-in inbox (`inbox`) is how an overdue hand-off is noticed.

**Answering from the person's own channel.** `platform/core/handoff_reply.py` is the way back from a delivered notice (#88, #97). The person replies `done <ref>`, `declined <ref> [note]` or `seen <ref>` with the reference from the notice, and the channel adapter passes the message it has already authenticated (channel, conversation id, verified sender id and whether the conversation is one-to-one) to `HandoffReplies.handle`, or to `runtime-handoff.py <store> reply --people people.json --channel slack --conversation <id> --sender <id> --direct "<text>"`. Identity comes from the channel and the people directory (an entry may also record the person's own `identity` on that channel, which the sender must match), never from the words typed. Only the assignee, or the named escalation contact once the hand-off is overdue, can answer; a shared channel, an unknown sender, an unknown or ambiguous reference and another person's hand-off all get the same neutral reply and a ledgered refusal that holds the reference and channel but never the message or the action's arguments. `done` and `declined` close the hand-off under the person's name and channel through the governed `complete_handoff` (a `tier: 4` ledger event with the person and `via` the channel), single use; for a human task step the note is returned to the task as data and never widens its authority. `seen` records an acknowledgement and only quiets the pre-deadline reminder: it never closes the hand-off and never stops the overdue escalation. The note is bounded to one line of 500 characters. Closing a human task step through `complete_handoff` previously failed because its request has no Tool arguments; it is ledgered now with the step's Tool name and empty arguments. **Not shipped:** wiring into the sidecar, a Slack, Teams or email adapter that calls it, and any live channel proof; until a deployment does, the operator commands remain the way to close a hand-off.

Delivery today is the built-in inbox (the operator commands above). Delivery to Slack, Teams or email, reminders/escalation when a deadline passes, and a person's confirmation arriving through their own channel are not implemented; the operator records the outcome on the person's behalf.

## Emergency pause (v0.8.75)

An owner can stop one AI coworker quickly without tearing the deployment down. The state is one file, `pause.json` on the governance volume (written atomically, mode 0600), holding who paused, when and why (the reason is trimmed to 300 characters, control characters removed). A file that exists but cannot be read, or is malformed, counts as **paused**: the control fails safe, never open. Nothing is stored per message and the state survives a restart of any container.

**Who can.** `pause coworker [reason]` and `resume coworker` in a *direct* chat, honoured only for a person the directory maps to the `admin` role; a member, a stranger and a shared channel all get the same neutral refusal and nothing changes. Only those exact words count (an ordinary sentence that mentions pausing goes to the coworker). The operator can run `client-operator.py pause|resume --project-name <env> --by <person> [--reason ...]` and `pause-status`. Both ways are ledgered (`coworker.pause`, `coworker.resume`: who, when, reason, via chat or operator).

**What stops.** At the shared governed boundary every Tool whose risk is not `read` (a Tool that cannot be found counts as material) is denied before an approval is claimed and the denial is ledgered with reason `coworker-paused`; an approval already granted stays unused until resume or its own expiry. The worker's scheduler, event, mailbox and CRM-sync jobs skip with `paused` (mail and events stay queued and nothing is dropped; reminders, notices and retention still run so people keep hearing about hand-offs and expiries). The gateway answers every incoming message with a holding message instead of calling the coworker (default text, or `pausedMessage` in the gateway config / `paused_message` in the EnvironmentBinding gateway block) and ledgers `channel.held-while-paused`. Hand-offs and reads still work, so a person can be told. The inbox shows a `PAUSED` item first, with who and why.

**In-flight and racing calls.** A call that has already passed the check completes; nothing new starts after it. There is no cancellation of a running upstream call, and a call that passed the check an instant before the file was written is not undone.

Honest limits: this is a per-client control, tested in-process and with fakes. It has not been proven against a live deployment (see [live proof](live-proof.md)), it does not stop actions a person takes directly in a provider, and spend caps and a status dashboard remain separate work.

## Gateway heartbeat and readiness (v0.8.76)

The channel gateway now writes the same payload-free heartbeat as the worker (`gateway/heartbeat.json` on the governance volume: a time, the fixed names of the periodic jobs that failed on the last pass, and those that failed on three passes in a row; shared code in `platform/core/heartbeat.py`, which the worker's `operational_health` also uses). It is written by the gateway's own loop every 15 seconds, so a wedged process is detected even though its port still answers. `python3 platform/runtime/gateway.py --readiness` prints `healthy`, `degraded` (a job failed three passes running) or `unavailable` (missing, malformed, a symlink, from the future, or older than 120 seconds) and exits non-zero unless healthy. The Docker health check is unchanged and is liveness only. A full or read-only volume does not stop the gateway; the missing beat is the signal.

Not done, and not claimed: Cloud Monitoring uptime checks and alert policies, notification channels and routing, deduplicated failure and recovery alerts, the optional voice runtime, runbooks and retirement controls, and a real notification drill (#231 stays open). The heartbeat is a file inside the private VM; nothing is exposed publicly.

## Chat task assignment in the gateway (v0.8.76)

A deployed gateway accepts `assign <agent>: <goal>` (or `delegate ...`) in a direct chat from a mapped person once `gateway.assign` is configured in the EnvironmentBinding: `assign: {permissions: {"<person>": ["<Tool name>", ...]}}` (the Tools that person may ask an Agent to use; a person with no entry cannot assign anything). The gateway builds a `TeamRuntime` over the governance volume with the Agent it serves, so the task tree (`tasks/`), the Agent's inbox (`inbox.json`) and the shared Action Ledger are the runtime's own, a restarted gateway sees them again, and the ledger records `task.assigned` under the person's name with the Agent and task id, never the goal. The delegation rules apply: the Agent can do only what it holds and the person may ask for, otherwise the refusal says so and nothing is stored. A stranger and a shared channel get the same neutral words. Without `assign` configured the words go to the coworker as before.

Not done, and not claimed (#229 stays open): the Agent starting on the task by itself, telling the person in the same channel when it reports done, declined or failed, task state shared between the gateway, worker and sidecar processes while they run, and any live evidence.

## Task report notices and adversarial control-surface checks (v0.8.77)

**Task reports reach the person (#229).** The worker's `task-reports` job (every 60 s, `intervals.task-reports`) reads the task inbox (`inbox.json` on the governance volume) and, once per task, queues a notice to the person who handed the task over when the Agent reports it `done`, `declined` or `failed`. The notice names only the task id, the Agent and the outcome; the report text stays in the task inbox and never enters the notice queue or the ledger. A task an Agent handed to a colleague is not announced to a person. The notice is delivered by the normal notices job to the person's own channel in the worker's `people` directory (a person who is not listed gets the usual undeliverable-notice handling). It keeps running while the coworker is paused, because a person is waiting to hear. This does not make the Agent start on a task by itself, share task state between running processes, or provide live evidence.

**Adversarial control-surface checks (#235, #232).** `evals/receptionist/adversarial.yaml` gains `controlCommands`, and `tests/test_control_surface_adversarial.py` sends them as a stranger, an ordinary member and an administrator in a shared channel, with the coworker already paused and not paused: the reply is always the same neutral refusal and the pause state never changes; a claim of authority in an earlier turn does not carry over; a paused coworker gives everyone, including a forged owner, only the holding reply; and a granted approval is neither consumed nor usable while paused, then still works once, after resume. These are deterministic tests of the governed outcome. No model is called, so they are not evidence that a model resists injection (a credential-backed red-team run is still required, and #235 stays open).

## Monthly spend cap (v0.8.78)

A first metering-side cost guard (#238), built on the emergency pause. Configure it per client in the EnvironmentBinding as `worker.monthly_spend_cap: {cap_cents: <whole US cents>, warn_at_percent: [50, 80]}` (rendered as the worker's `spend` setting; `warn_at_percent` is up to five percentages from 1 to 99, default `[80]`). The worker's `spend` job (every 300 s, `intervals.spend`) adds up the `costMicrosUsd` of the `usage.recorded` ledger events dated in the current **UTC calendar month**. That is the cost Hermes estimated for each session, so it is a guard rail and not an invoice, and it can differ from what is billed (rate cards, unbilled or late usage, a different month boundary or currency).

At each configured percentage the owner (`escalateTo`, else the mailbox owner) gets one notice per month (`spend.warning` is ledgered). At the cap the worker pauses the coworker with the same state the owner's pause uses (`pause.json`), marked `cause: spend-cap` and the month, so new material Tool calls are denied at the shared governed boundary, the worker's schedules, events, mailbox and CRM filing wait, and the gateway gives the holding reply; `spend.cap-reached` is ledgered and the owner is told once. Status, the inbox, audit, hand-offs and notices keep working. A cap pause is re-applied while spending is still at or over the cap, so `resume coworker` alone does not outrun it: raise `cap_cents` and redeploy, or wait for next month. When a new month starts under the cap the worker clears a pause **it** caused (`spend.cap-cleared`); a pause an owner made is never cleared by the worker, and an owner's pause that is already in place is not relabelled.

Not done, and not claimed (#238 stays open): Lago-side thresholds, per-client timezones and billing windows, per-coworker caps and attribution, a cap-change history, enforcement of a call already running when the cap is reached (the usual in-flight rule applies, and usage is metered every few minutes, so the cap can be exceeded by what is spent between passes), reconciliation against invoices, and live proof of the alert route.

## Weekly owner digest and worker failure alerts (v0.8.79)

**Weekly digest (#236, first slice).** Opt in per client with `worker.weekly_digest: {to: <person in the worker's people directory>, weekday: 0-6 (Monday is 0), hour: 0-23, utc_offset_minutes: -720..840}` (rendered as the worker's `digest` setting; defaults Monday 08:00 at UTC). The worker's `digest` job (checked hourly, `intervals.digest`) sends the named owner one short notice through their own channel, once per window and idempotent across restarts (`digest-sent.json`): the seven days before the most recent configured weekday and hour (window end exclusive). It counts what the ledger recorded: actions performed and failed, approvals requested, approved and rejected, denials and hand-offs, metered model sessions by channel (the closest thing to conversations the ledger has) and their approximate cost (a Hermes estimate, not an invoice), plus what is waiting now from the inbox (approvals, hand-offs and overdue ones, tickets, CRM filings blocked, and whether the coworker is paused). It holds counts only, never a message, customer identity, file or tool argument. With no activity it still sends zeros. A window missed by more than two days (the worker was down) is skipped, not back-filled. `digest.sent` is ledgered under `runtime:worker` with the recipient and the counts. The timezone is a fixed UTC offset (no daylight saving), and the digest is per client, not per coworker.

Not done (#236 stays open): named timezones with daylight saving, a verified-recipient and change control beyond the people directory, per-coworker and per-channel conversation counts from the conversation store, resolved versus escalated requests, and live delivery.

**Worker failure and recovery alerts (#231, in-product part).** When a worker job has failed three passes in a row, the escalation contact (`escalateTo`, else the mailbox owner) gets one notice naming only the job, and one more when the job works again. The state is kept in `worker/alerts.json`, so flapping is deduplicated by state rather than by time, a restart that forgets the streaks is not announced as a recovery (only a job that ran and succeeded is), and the notice carries no error detail. With nobody to tell, nothing is queued. This complements the heartbeat and `--readiness`; it is not Cloud Monitoring, not provider-native alert routing, and not proof that a real alert reaches a person (#231 stays open for those).

## Enterprise audit contract

Governed material actions emit versioned JSONL records with `schemaVersion: 3`. Depending on the stage, records capture:

- timestamp and event type (`authorization`, `approval`, `execution`);
- decision/outcome;
- immutable governed-action `requestId`;
- Agent and Tool;
- initiating user/workload when available;
- bounded governance context such as tenant, environment, deployment, session/conversation, runtime/model provenance and correlation ID;
- optional commercial attribution context such as cost center, billing subscription and shared consumption pool;
- approver identity for approved actions;
- result type plus a digest of the result rather than the full connector response;
- failure type without copying exception messages that may contain sensitive provider data;
- monotonically increasing sequence, previous-event hash and current-event SHA-256 hash.

Schema v3 adds the fields needed to answer "what did the AI do and why" without trusting the AI's narrative:

- **Arguments redacted plus digest.** `arguments` keeps only fields the capability catalog marks safe (for Calendar: `start`, `end`, `timezone`); everything else, including summaries, descriptions and attendees, is `"[redacted]"`. `argumentsDigest` is the SHA-256 of the exact canonical arguments, so an incident reviewer holding the original request can prove a match. Tools outside the catalog have every argument redacted.
- **Authority fields in `governance`** (bound into the `requestId`, so an approval cannot be replayed under a different authority): `originatingPrincipal`, `channel`, `triggerRef`, `taskId`, `parentRequestId` and `delegationChain[]` (1–16 hops).
- **Descriptive fields in `ledger`** (not bound into the `requestId`, so a reworded justification or a new release never invalidates a pending approval): `agentVersion` (digest of the compiled desired state), `workspaceRevision` (`MODEL_GARDEN_WORKSPACE_REVISION`), `platformRelease` (`VERSION`), `policyRuleId` + `policyVersion` (the governance rule that decided), `justification` (the model's stated reason, whitespace-collapsed and capped at 500 characters), `capability`, `provider`, `tier` and `externalObjectRef` (for example `google-calendar:event/<id>` on a successful booking).

Every new field is allow-listed and length-bounded; anything else (prompts, transcripts, credentials, chain-of-thought) is rejected. `verify_audit_chain()` verifies v1 prefixes and v2/v3 chained records together without rewriting history, and rejects a chain whose schema version moves backwards.

### Read events

Successful `read`-class Tool calls and `kb.search`-style knowledge reads (`record_knowledge_read()`) emit an extra `read` event recording what the coworker knew: source ID, classification, record/document IDs and per-record content digests, never content. Read events carry the same `governance` block (`taskId`, `correlationId`) as the writes that follow, so a timeline can join them.

Volume is configurable per client with `audit.reads` in the EnvironmentBinding (passed to the sidecar as `MODEL_GARDEN_READ_AUDIT`):

- `full` (**default**): one read event per read call, with up to 100 record IDs (`recordsTruncated` marks the cap) plus `recordCount` and a digest over the full list;
- `aggregate`: record IDs are omitted; `recordCount` and the list digest remain;
- `sampled:<N>`: read events for about 1 in N calls, chosen deterministically from the `requestId`.

Sources classified `restricted` are always recorded with every record ID, whatever the mode. The `execution` event for every call is recorded regardless of read sampling.

Audit appends use an exclusive filesystem lock, flush plus `fsync`, a restrictive `0600` file mode and a `0700` parent directory. `verify_audit_chain()` checks ordering and the SHA-256 chain and supports a legacy schema-v1 prefix without rewriting historical records. Modification, deletion from the middle, reordering or insertion of chained records is therefore detectable from the retained chain/head hash.

This is **tamper-evidence, not immutable storage**. An administrator with host/storage authority could still replace or delete the complete local log and its head. Enterprise deployments that require independent non-repudiation or longer retention should continuously export governed-action records/head hashes to a separately administered immutable log/SIEM/object store according to the client retention policy.

The bounded context intentionally rejects arbitrary fields so prompts, raw credentials and uncontrolled application payloads cannot be smuggled into the governance envelope.

### Ledger export and verification

`scripts/ledger-export.py` (library: `platform/core/ledger_export.py`) copies the chained records and head manifests to a sink that the runtime identity should not be able to rewrite:

```bash
python3 scripts/ledger-export.py export --ledger /opt/data/.modelgarden/audit.jsonl \
  --state /var/lib/model-garden/export-state.json --sink-dir /mnt/ledger-sink
python3 scripts/ledger-export.py export ... --webhook https://siem.example/ingest --token-env SIEM_TOKEN
python3 scripts/ledger-export.py verify --ledger /opt/data/.modelgarden/audit.jsonl --sink-dir /mnt/ledger-sink
```

- **Delivery:** at-least-once. The record ID is the event's own chain hash, so a duplicate delivery is harmless and a different record under an existing ID is an error. The cursor only advances after the sink acknowledges a batch.
- **Sinks:** an append-only directory layout (files created exclusively, never rewritten; mount it where a different identity controls writes) and a generic HTTPS collector with an `Idempotency-Key` per batch. The collector is write-only, so `verify` needs the directory sink. `--gcs-bucket` writes to a Google Cloud Storage bucket create-only (`ifGenerationMatch=0`, so an object is never replaced and a repeat is a harmless duplicate), optionally as a separate export identity the VM impersonates (`--gcs-impersonate`); `verify --gcs-bucket` reads it back as a separate verifier identity.
- **Delivery alarms:** `verify --max-lag-events N --max-head-age-seconds S` also alerts when the exporter has stalled: the local ledger is more than N events ahead of the export, or events are waiting and the newest head manifest is older than S seconds (or none was ever exported). A fully exported idle ledger never trips either, and both are off unless set.
- **Alerts instead of a non-zero exit.** The runtime worker alerts the binding's escalation contact through the notice channel when its own export pass finds the local ledger no longer matches the export (`export-integrity`), or when exporting keeps failing so events pile up (`export-lag` past 100 events, `export-stale` after six hours); the runtime cannot read the bucket, so full verification stays with the verifier identity. For that verifier, `platform/core/export_monitor.py` (`ExportMonitor.check`) runs the same verification and turns the result into owner notices for `notice_delivery.py`: `export-integrity` (a broken or truncated chain, a gap, a local ledger that differs from the export, a missing or mismatched head manifest; says the export cannot be trusted until a person checks it), `export-lag` and `export-stale`. A condition alerts once when it starts, again only after a reminder interval (default six hours) while it persists, and one `export-recovered` notice follows when everything clears; state is kept in a private file so a restart does not repeat an alert, and a notice that was not delivered is offered again. Notices name the condition, how many events behind and a recovery step, never ledger content or paths; with no owner they are `unowned` and stay loud in delivery. It sends nothing and is not wired to a timer or the sidecar, and an export that was never set up (empty ledger, empty sink) raises nothing.
- **Verification:** recomputes the chain from the exported records alone and reports gaps in the exported sequence, a local log that is shorter than the export (truncation) or that differs from it (replacement with another valid chain), tampered exported records and a head manifest that disagrees with its record. It prints `ALERT:` lines and exits 2; routing that to monitoring is deployment-specific.
- **Reference target (v0.8.23).** `infra/docker-host/gcp/ledger-export.tf` (opt-in `ledger_export_enabled`) creates a bucket with public access prevented, uniform access and a retention policy of `export_retention_days` (locked with `export_lock_retention`, which prod requires and which cannot be undone), plus a separate service account that can only create objects. The runtime account has no role on the bucket and may only mint tokens for that account; `export_verifier_members` names who may read the copy, and may not include the runtime. The bootstrap's `enable_ledger_export` lets the deployer create these. With `worker.ledger_export` in the binding (`gcs_bucket`, `impersonate_service_account`, `prefix`) the governed worker runs the export every five minutes and a failed pass is ledgered as a worker job failure. Verification needs the separate verifier identity, so it runs where that identity does.
- **Not proven:** the bucket, the lock and the verifier have not been run against a real project, and the verifier's own alert routing (`ExportMonitor.check` on a timer outside the runtime) is deployment-specific. Until a client environment runs this against a separately administered target, the ledger remains tamper-evident, not independently immutable.

### Audit-to-cost correlation

Audit and billing remain separate systems with a stable join key. A usage event may include:

- `action_request_id` = the governed Tool `requestId`;
- `trace_id` = the same safe runtime/call correlation identifier used in governance context;
- client, Agent, environment, provider, cost center, billing subscription and shared-pool dimensions where applicable.

This enables enterprise showback and incident reconstruction without placing pricing logic in the Agent runtime or copying prompts/transcripts into the billing system. See `docs/metering.md` and `contracts/v1/usage-attribution.schema.json`.

A shared consumption pool is only a commercial/accounting grouping. It never weakens per-Agent action audit, approval rules or client/environment isolation.

### Audit query, task timeline and replay (v0.8.19)

`client-operator.py audit` asks the Action Ledger without touching a live system (`platform/core/audit_query.py`). Give it `--ledger <file>` (the ledger or an export) or `--project-name` to read the running client's ledger; it verifies the hash chain first and refuses a ledger that does not verify unless `--allow-unverified` is passed.

- `audit query` filters by `--agent`, `--principal`, `--approver`, `--capability`, `--provider`, `--tool`, `--event-type`, `--outcome`, `--since`/`--until`, `--task`, `--correlation`, `--external-object` and `--request`.
- `audit timeline --task T` (or `--correlation C`) joins reads, delegation chains and hops, approvals, executions, hand-offs, a person's task-step answer, an exhausted budget and any usage (`tokens`, `costCents`, `steps`) the events report across Agents, in ledger order.
- `audit replay-policy --desired-state <compiled state>` re-evaluates each recorded authorization against the current or a historical compiled policy and lists the differences. Arguments are redacted in the ledger, so when a policy has conditional rules the affected results are marked `inputsIncomplete` rather than claimed as matches. The access level actually applied is now recorded on each event (`ledger.principalAccess`).
- `audit replay-dry-run` walks recorded executions against sandbox connectors only; anything that is not a `SandboxConnector` is refused when the replay is built, it writes nothing to the ledger, and steps with redacted arguments are skipped unless the sandbox accepts them. The CLI uses a mock that makes no network call.
- `--format csv` (spreadsheet formulas neutralised) or `--format json`, with `--output`, gives DDQ and auditor evidence.

What remains is evidence, not code: a real multi-Agent client producing delegation chains (#71), and attributing each action to the external provider's acting identity (#129).

## Current evidence boundary

Credential-free tests cover capability resolution and the manifest, ledger v3 bounds and redaction, read-event digests, channel principal mapping, Tool selection, MCP materialization, governance, versioned action audit, audit-chain tamper detection, approval identity, single-use approval behavior, bounded cost-attribution context, governance-volume isolation, fail-closed Calendar routing, governed upstream MCP/API execution and Docker credential isolation. Metering tests cover canonical client/Agent/environment/provider attribution, shared-pool/subscription dimensions rejection of obvious sensitive billing properties, exact per-session usage deltas from the Hermes stores, and the billing sync's chain checks, idempotent Lago events and per-Agent report.

A real approved upstream Google/Microsoft calendar end-to-end booking with separate coworker authorization, external audit export/retention where required, and production reconciliation from provider usage -> Lago -> customer subscription remain live acceptance gates and must not be claimed complete until exercised in a deployed client environment.

## MCP connection readiness and upstream receipts (v0.8.12)

Tenant operators can inspect **non-secret, per-coworker** upstream readiness without making a network request:

```sh
python3 scripts/runtime-mcp-connections.py /opt/data/.modelgarden --tenant acme-prod status --agent receptionist --check-grants
```

The report shows declared portable Tools, their risk and whether that coworker's **own** active grant covers each operation; private server URLs, grant secret refs, acting identities and activation tokens are omitted. States distinguish `inactive`, `review-required`, `grant-unavailable` and `configured-awaiting-upstream-proof`. The last state is **not** a claim that the provider is connected or safe to mark live: reviewed TLS-pinned upstream transport, consent custody and tenant verification remain separate gates (#87, #86, #159).

The MCP execution seam now treats a protocol `isError: true` result as a failed execution, retaining the existing redacted failure ledger path rather than reporting a successful side effect. Only short, opaque, structurally constrained external object references may be copied into the audit ledger; provider prose, queries and arbitrary object fields never become `externalObjectRef`. These are provider-neutral safety rules and add no native SaaS connectors.

**Tier 0 tickets (v0.8.38).** The sidecar serves `tickets.search`, `tickets.create` and `tickets.update` from a private per-client work-item list when `tickets` is bound to the credential-free `built-in-tickets` provider (see "Tier 0 work items" in the contracts): staff see and update every ticket, anyone else only searches their own, and the ledger records project, id and status, never the text. The runtime inbox counts open tickets and `scripts/runtime-tickets.py` lists, shows and updates them on the client host.

**Tier 0 files (v0.8.21).** The sidecar serves `files.search`, `files.read`, `files.write` and `docs.read` from the same private artifact store as `docs.create`, for the verified requester only (a person sees just their own files; there are no folders, and binary DOCX/XLSX files show as metadata). `files` binds to the credential-free `built-in-files` provider and `docs` to `built-in-artifacts`; unbound, the tools answer with the human hand-off wording. Writes need exact-action approval; the ledger holds the `tier0:<tenant>/<id>` location, never names or contents. A live Drive or SharePoint grant still replaces these when one is connected (#91).
