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
