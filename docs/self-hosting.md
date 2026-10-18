# Self-hosting and low-cost edge strategy

Model Garden is OSS-first, not self-host-at-any-cost. The operating rule is to minimize total cost of ownership while preserving portability, client isolation, first-party governance and a simple recovery path.

Use this order when deciding where a component runs:

1. Avoid the component if the MVP does not need it.
2. Reuse the client's existing system when that is sufficient.
3. Prefer standards and mature OSS behind a thin Model Garden boundary.
4. Self-host OSS when it materially reduces recurring cost, improves control/data residency, or avoids lock-in without creating disproportionate operational burden.
5. Use a managed service when its free/included tier or operational leverage is cheaper than running another production service ourselves.
6. Re-evaluate with measured usage before adding infrastructure.

## Current hosting shape

The supported early-client path remains:

```text
private client workspace
        |
        v
GitHub Actions + GCP workload identity
        |
        v
isolated per-client GCP Docker host
        |
        +-- Hermes Agent
        +-- Model Garden governed MCP Tools
        +-- client-scoped persistent runtime/governance volumes
```

The current GCP reference intentionally does not deploy Kubernetes, a shared control plane, a custom model gateway, LiveKit infrastructure, Lago infrastructure or a portal.

## Runtime worker

`EnvironmentBinding.spec.worker` (opt-in, `enabled: true`) adds two services to the Docker bundle:

- `governed-worker` (`platform/runtime/worker.py`) shares the governance volume with the governed sidecar and is the clock for work nobody prompts: it applies the workspace's `Schedule` definitions on start under `MODEL_GARDEN_DEPLOYED_BY`, runs due schedules and resumes tasks whose human step was answered, drains the built-in inbound mail spool into the support mailbox (and fires `mail.received` and `ticket.created` event triggers for what it handled), delivers hand-off, human-step, connection, grant, schedule and mailbox notices through each person's own channel, and purges expired artifacts and mail. A failing job is ledgered as `worker.job-failed` (job and error type only) and never stops the others; `worker.py --health` is the container health check and `--once` runs one pass for an operator.
- `coworker-api`, present when `coworker_api_key_ref` is set, is a second Hermes process (`hermes gateway run`) serving only its OpenAI-compatible API server on the private Compose network with `API_SERVER_KEY`. The worker asks the coworker through it (`platform/runtime/coworker_client.py`) and never holds a model credential; the client refuses any non-private URL.

```yaml
worker:
  enabled: true
  coworker_api_key_ref: secret://coworker-api/dev     # API_SERVER_KEY for the private coworker API
  owners: {jordan@client.example: ["*"]}               # whose schedules may use which Tools; absent means none
  people: {jordan@client.example: {channel: slack, address: D0123}}  # where each named person is told things
  escalate_to: ops@client.example
  mailbox: {addresses: [support@client.example], trusted_authserv_id: mx.client.example, owner: jordan@client.example}
  intervals: {scheduler: 60, mailbox: 30, notices: 60, retention: 3600}
```

The worker and the gateway keep separate `people` and `escalate_to` settings, but when both are enabled a side that omits them takes the other's, so filling in either list is enough for notices to reach people. If both give `people` and one person has a different channel or address in each, the deploy fails with an error naming that person.

Customer email answered by the worker is drafted from approved knowledge only and kept as a Tier 0 draft; the mailbox owner is told it is ready and sends it. Inbound mail still reaches the spool through the mail server's pipe (`scripts/runtime-mailbox.py ingest`).

## Channel gateway

`EnvironmentBinding.spec.gateway` (opt-in, `enabled: true`) adds the `channel-gateway` service (`platform/runtime/gateway.py`): the HTTP and SMTP front door where people reach the coworker. It shares the governance volume, asks the coworker through the same private Hermes API as the worker, and holds no model credential.

- `/chat` is the built-in web chat. Sign-in is a magic link whose token is in the URL fragment, so a mail scanner that opens the link spends nothing; `POST /channels/web/login` answers the same for listed and unlisted addresses. The page ships its own script and style under a CSP that allows nothing else.
- `/channels/slack/events` and `/channels/slack/interactive` verify Slack's signature before reading anything, answer direct messages and allow-listed `@mentions` in thread, keep files shared by mapped people as Tier 0 artifacts, and turn Approve/Reject buttons into decisions by mapped approvers only.
- `/channels/teams/messages` verifies the Bot Framework token (RS256 against Microsoft's published keys, then issuer, audience, tenant and service URL) and handles messages and Adaptive Card approvals.
- `/approvals/confirm` is the emailed-approval page: preview on GET, decide only on POST.
- Hand-off replies (`done <ref>`) and schedule commands (`schedule create ...`) typed in a verified direct conversation are routed to their modules under the channel identity; anything else goes to the coworker with the sender's access level. Pending approvals are announced once as buttons in each `approvals.notify` conversation.
- With `mailbox`, an SMTP listener (port 2525) feeds the mail spool. It is not a relay (recipients outside the configured addresses get 550), strips any `Authentication-Results` it did not get from a configured `trusted_relays` address, and has no TLS or AUTH: put the client's MTA or a TLS proxy in front.

Ports are published on loopback by default (`bind`, `smtp_bind`); a TLS reverse proxy is the public listener. On the GCP reference target that listener is the opt-in load balancer in `infra/docker-host/gcp/gateway-ingress.tf` (see "Public gateway ingress" below); set `bind: 0.0.0.0` in the binding when you enable it, because the load balancer reaches the VM's interface and the firewall rule (Google front-end ranges only, tcp:8080) is what keeps the port private. Secrets are `secret://` references injected at apply time, never rendered into the bundle.

```yaml
gateway:
  enabled: true
  public_origin: https://chat.client.example
  coworker_api_key_ref: secret://coworker-api/dev      # same key as worker.coworker_api_key_ref
  secrets: {slack_signing_secret_ref: secret://slack/signing, slack_bot_token_ref: secret://slack/bot, webchat_key_ref: secret://webchat/key}
  directory: {slack: {T01/U123: {identity: jordan@client.example, roles: [approver]}}}
  people: {jordan@client.example: {channel: slack, address: D0123, identity: U123}}
  slack: {bot_user_id: U0BOT0001, allowed_channels: [C0123]}
  web: {allowed_addresses: [jordan@client.example], mail: {from: chat@client.example, host: smtp.client.example}}
  approvals: {notify: [{channel: slack, conversation: D0123}]}   # also where action receipts go; receipts: false turns them off
  mailbox: {addresses: [support@client.example], trusted_relays: [10.0.0.5]}
```

It is not live-proven: no real Slack app, Teams bot, mail provider or browser session has exercised it.

### Public gateway ingress (GCP)

`infra/docker-host/gcp/gateway-ingress.tf` is off by default. With `gateway_ingress_enabled = true` it adds a static global IP, a Google-managed certificate for `gateway_domain`, a global external HTTPS load balancer to the VM on port 8080 (health check `GET /healthz`), an HTTP to HTTPS redirect, and a firewall rule allowing only `130.211.0.0/22` and `35.191.0.0/16` to tcp:8080 on the runtime tag. The VM still has no external IP.

| Variable | Default | Meaning |
| --- | --- | --- |
| `gateway_ingress_enabled` | `false` | Create the load balancer and its firewall rule. |
| `gateway_domain` | `""` | Required when enabled; the public hostname (also the certificate name). |
| `gateway_dns_zone` | `""` | Optional Cloud DNS managed zone in the project; when set, the A record is created. Needs the bootstrap's `enable_gateway_dns`. |
| `gateway_smtp_enabled` | `false` | Also expose public tcp:25 to the container's 2525 through a TCP proxy load balancer. |
| `gateway_smtp_source_ranges` | `[]` | Required non-empty when SMTP is enabled (at most 10 CIDRs, never `0.0.0.0/0`); enforced by a Cloud Armor allow-list with default deny. |

Outputs: `gateway_public_ip`, `gateway_url` (use as `gateway.public_origin`) and `gateway_smtp_ip`. The deployer already accepts `bind: 0.0.0.0` (any IPv4), so no deployer change is needed; set `gateway.bind: 0.0.0.0` and, for mail, `gateway.smtp_bind: 0.0.0.0`. Slack and Teams callbacks are not IP-restricted: their signature and token checks are the control. The SMTP path is unauthenticated and plaintext, so allow-list only your MTA's addresses. See `docs/cloud-bootstrap.md` for the DNS and certificate walkthrough.

## Component decisions

| Component | Current status | Default decision | Why |
| --- | --- | --- | --- |
| Hermes Agent | Self-hosted on the client GCP Docker host | Self-host now | It is the reference OSS Agent Runtime and already fits the isolated Docker target. |
| Model Garden governed MCP Tools | Self-hosted with Hermes, in a separate sidecar/authority boundary | Self-host now | Governance and approval semantics are a first-party Model Garden contract. |
| Model inference | OpenAI/Anthropic credentials in the current production target | Managed for MVP; evaluate self-hosted inference later | Low-volume API inference is operationally simpler than running an always-on GPU. Add an open-weight endpoint only when privacy, throughput or measured cost justifies it. |
| Google Calendar | External Google API | External | It is the client's source system, not a Model Garden service to reproduce. |
| LiveKit realtime media | Replaceable adapter; infrastructure not provisioned by Model Garden today | Use LiveKit Cloud free/included capacity for the first live proof; self-host when evidence justifies it | Self-hosting is supported upstream but requires a public realtime host, TLS/TURN, Redis, upgrades and broad UDP exposure. The managed free tier can be cheaper than an additional GCP VM at MVP volume. |
| SIP/PSTN | External telephony path | External paid dependency | Even with a self-hosted SIP server, a real phone number/PSTN carrier remains external. |
| Lago | Thin usage-event adapter only; no shared deployment yet | Self-host later when billing is actually needed | One shared Model Garden-operated Lago is preferable to one instance per client, but deploying billing infrastructure before billable usage exists is unnecessary. |
| Observability | Existing logs/audit/evals first | Add OSS observability only when current evidence is insufficient | Do not add a telemetry stack solely because it is available. |

## LiveKit: when to self-host

LiveKit's server and SIP service can be self-hosted on a VM. A production-shaped self-hosted deployment needs more than a container:

- a stable domain and trusted TLS certificate;
- a publicly reachable media host;
- WebRTC TCP/UDP and TURN connectivity;
- Redis for production LiveKit/SIP coordination;
- SIP signalling plus a public RTP range for telephony;
- upgrades, health checks, monitoring and recovery;
- a PSTN/SIP carrier for actual telephone numbers/calls.

Do not place the LiveKit media or SIP path behind Cloudflare Tunnel or Cloudflare's normal HTTP proxy. WebRTC/SIP requires direct TCP/UDP connectivity; Cloudflare Spectrum support for arbitrary TCP/UDP is not part of the Free plan. Cloudflare may still provide authoritative DNS for the LiveKit/TURN/SIP hostnames, but those records should be DNS-only when the underlying protocol must reach the GCP host directly.

For the first callable reference employee, prefer the lowest-TCO path that proves the product. As of 2026-09-17, LiveKit Cloud's free Build tier includes meaningful agent/media/telephony allowances, including one US local number and a limited inbound-minute allowance. Re-check current limits before depending on them.

Self-host LiveKit after one or more of these becomes true:

- managed media/telephony cost is materially higher than the GCP and operator cost;
- client data-residency/privacy requirements require our infrastructure;
- call volume is stable enough to size capacity from evidence;
- managed-plan limits block the required product behaviour;
- we can demonstrate backup/restore, monitoring, upgrade and incident procedures for the voice stack.

When that point arrives, prefer a dedicated voice/media target rather than silently opening broad public media ports on the existing client runtime host. Preserve the same LiveKit adapter contract so moving between managed and self-hosted LiveKit does not rewrite Agents or Skills.

## Lago: self-host centrally, not per client

The application boundary already supports a configurable Lago base URL. When Model Garden begins charging from metered usage, deploy one shared Model Garden-operated Lago service rather than adding Lago to every client runtime.

Keep the separation:

```text
client runtime -> attributable usage event -> shared Lago -> commercial billing
       |
       +-------------------------------------> first-party governance/audit remains separate
```

Billing must never become the source of truth for what the AI did or who authorized it.

Before production billing, prove deterministic event deduplication, reconcile sample usage against provider statements, back up the billing database, and test restore/upgrade procedures.

## Cloudflare Free: useful boundaries

Cloudflare should reduce cost or public exposure without becoming Model Garden's authority layer.

### Use now or early

**Authoritative DNS.** Cloudflare provides free authoritative DNS and does not charge Free/Pro/Business customers per DNS query. This is a good default for Model Garden-owned domains and future service subdomains.

**DNS-only records for realtime services.** A self-hosted LiveKit/TURN/SIP deployment may use Cloudflare DNS, but realtime records must remain directly reachable where the protocol requires it. Do not assume the orange-cloud HTTP proxy can carry WebRTC or SIP.

### Use when an HTTP admin/service surface appears

**Cloudflare Tunnel.** Tunnel is useful for publishing an HTTP service from GCP without opening an inbound application port or buying a GCP HTTP load balancer. Good candidates include a future shared Lago UI/API, internal operations UI, or another low-volume HTTP admin surface.

**Cloudflare Access.** Put human-only admin surfaces behind Access where appropriate. The current Cloudflare Zero Trust Free plan is intended for teams up to 50 users; verify current plan terms before production reliance.

Keep the underlying service portable: Tunnel/Access should be an edge/access adapter, not the only place business authority or durable state exists.

### Consider later

**R2.** As of 2026-09-17, R2 Standard includes a monthly free tier (10 GB-month storage, 1M Class A operations and 10M Class B operations) and no Internet egress charge. It can be useful for non-authoritative artifacts, exports or backups when that actually saves money.

Do not move Terraform state, approval state or first-party governance evidence to R2 merely to consume a free tier. Existing GCS remote state is small, integrated with the current GCP trust model and not a meaningful cost bottleneck at MVP scale.

**Workers.** The Workers Free tier is suitable for small stateless HTTP glue. Do not move approval, policy enforcement, tenant authority or billing truth into a Worker to save a few dollars. If a Worker is ever used for a security-sensitive edge function, use fail-closed behaviour and retain a portable origin contract.

### Do not use Free-tier Cloudflare for

- arbitrary UDP/TCP proxying for LiveKit/SIP;
- replacing a PSTN/SIP carrier;
- general outbound Internet/NAT for GCP VMs;
- durable approval/governance authority;
- a new central control plane before live-client evidence demands one.

## Cost drivers to measure

Do not invent exact monthly bills before there is traffic. Track the cost drivers that actually determine the answer:

- GCP VM uptime, machine type, disks and outbound network;
- model tokens/requests or future GPU utilization;
- PSTN number rental and call minutes;
- realtime participant/media minutes and bandwidth;
- storage/backup retention;
- operator time for upgrades, incidents and restores;
- billing/observability volume once those services exist.

The cheapest service on a price sheet is not cheaper if it adds an always-on VM or significant operator burden.

## Next deployment sequence

1. Keep Hermes and governed Tools on the existing isolated GCP Docker host.
2. Use the lowest-TCO LiveKit path to complete the first real inbound-call proof; the managed free tier is acceptable because the adapter remains replaceable.
3. Use Cloudflare free authoritative DNS where Model Garden controls the domain; do not proxy the realtime media/SIP path.
4. Prove model -> Hermes -> governed Tool -> approval -> Google Calendar -> audit evidence in the live environment.
5. Prove the phone path, human fallback and representative live evals.
6. Measure actual model, media, telephony and GCP usage.
7. Only then decide whether LiveKit self-hosting or open-weight inference reduces total cost.
8. Deploy shared self-hosted Lago only when commercial metering/billing becomes an active requirement; front its HTTP surface with Tunnel/Access if that meaningfully reduces GCP exposure/cost.

## Vendor facts to re-verify before deployment

These choices rely on vendor plans and network requirements that can change. Re-check current official documentation before each production rollout:

- Cloudflare DNS: <https://developers.cloudflare.com/dns/faq/>
- Cloudflare Tunnel: <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/>
- Cloudflare Spectrum plan availability: <https://developers.cloudflare.com/spectrum/protocols-per-plan/>
- Cloudflare R2 pricing: <https://developers.cloudflare.com/r2/pricing/>
- Cloudflare Workers limits: <https://developers.cloudflare.com/workers/platform/limits/>
- LiveKit self-hosting: <https://docs.livekit.io/transport/self-hosting/>
- LiveKit VM/firewall requirements: <https://docs.livekit.io/transport/self-hosting/vm/>
- LiveKit SIP self-hosting: <https://docs.livekit.io/transport/self-hosting/sip-server/>
- LiveKit Cloud quotas: <https://docs.livekit.io/deploy/admin/quotas-and-limits/>
- Lago self-hosting: <https://getlago.com/solutions/use-cases/self-hosted>
