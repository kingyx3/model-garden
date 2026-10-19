# Implementation architecture

This document owns the **repository-side implementation architecture** for the currently supported Model Garden release. Business/product invariants, build-vs-reuse decisions and future platform expansion belong in Confluence: [Business Model](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/327686/Model+Garden+Business+Model+Minimum+Viable+Platform), [Build Plan](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/917506/Model+Garden+Build+Plan+Hermes+vs+Model+Garden+Responsibilities) and [Roadmap](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/950302/Model+Garden+Roadmap+MVP+to+Ideal+State).

## Current implementation

```text
private client workspace
Agent + selected Skills/Tools + Knowledge refs + evals
                     |
                     v
             modelgarden.ai/v1
                     |
             validate / compile
                     |
                     v
          Hermes runtime profile
                     |
             governed MCP Tools
                     |
    Tier 0 built-ins or approved upstream MCP/API

cloud bootstrap (once)
  -> remote Terraform state
  -> GitHub OIDC / workload federation
  -> deploy + runtime identities
                     |
                     v
Terraform -> isolated client Docker host
```

## Implementation boundaries

| Seam | Current implementation |
| --- | --- |
| Client desired state | One separate private workspace repository per client, pinned to a published Model Garden release. |
| Validation/materialization | Versioned `modelgarden.ai/v1` contracts, deterministic workspace validation/compiler and Hermes profile materialization. |
| Agent runtime | Hermes reference runtime; runtime implementation remains behind the Model Garden client contract. |
| Business actions | Governed MCP Tool boundary with explicit Tool selection, `allow | approval | deny`, exact-action approval, per-coworker scoped grant records, secure operator-assisted credential/OAuth enrollment and tamper-evident audit. These controls have offline tests; independent acting identities and revocation must still be proven against real providers (#129/#86). |
| Business-system integration | Provider-neutral capabilities and a canonical [integration registry](../contracts/v1/integrations.json). Tier 0 knowledge, contacts, files, artifacts and tickets have credential-free tests. The governed sidecar supports approved, pinned upstream MCP transport and narrowly defined client REST operations; custom declarations and owner-entered secrets can be staged using operator-assisted flows. Bespoke Google/Microsoft Calendar, Gmail/Outlook, HubSpot/Salesforce and Drive API clients were retired. External SaaS routes remain **planned, not tenant-live**, until approved grants, source-provider identity/credential lifecycle and real integration proof pass the [provider evidence gate](contracts.md#provider-evidence-manifest-and-go-live-gate). |
| Channels | Verified-principal Slack, Teams, web chat and email channel adapters plus a gateway/SMTP intake are implemented and offline-tested, but real provider/workspace acceptance remains open (#82–#84). LiveKit has tested two-way transcript storage, model-or-fallback post-call summaries and operator-invoked governed CRM filing; complete live SIP and unattended end-to-end proof remain open (#107/#108). |
| Usage/cost attribution | Model tokens (from Hermes' session counters) and governed Tool calls ledgered per client, Agent and channel, exported with the ledger, and synced by the shared billing host to self-hosted Lago (`infra/billing/gcp`). |
| Infrastructure | Terraform-managed keyless GCP bootstrap and isolated Compute Engine Docker host. |
| Delivery control | GitHub PR validation plus generated keyless environment deployment workflow. |
| Skills and coworker identities | Individual Skill selection, disablement, dependency readiness **and first-class reusable Skillsets** ([#128, completed](https://github.com/kingyx3/model-garden/issues/128)) are implemented. Multi-Agent bundle rendering (`scripts/deploy-team-docker.py`, one runtime and attested identity per Agent) and the in-process team runtime with attenuated delegation, a per-task budget charged as calls happen and human steps (`platform/runtime/team_runtime.py`; see [contracts](contracts.md#team-of-agents)) are implemented credential-free and unproven against a live client (#95, #96, #97, #98). Independently scoped provider grant records and secure enrollment exist, but live provider-side coworker identity isolation (#129) and client self-service Studio (#130) remain evidence-gated work. |

## Deployment contract

- The shared Model Garden repository contains platform code; client business desired state stays in the separate private client workspace.
- GitHub controls tested software promotion; Terraform controls infrastructure convergence; Model Garden controls runtime convergence.
- Initial GCP bootstrap may use one temporary local service-account credential. Normal deployment uses GitHub OIDC / Workload Identity Federation after bootstrap verification.
- Deployment and runtime identities are separate. Raw runtime credentials do not belong in workspace business content or Terraform state.
- DEV and PROD are separate deployment/secret boundaries and production promotion uses the tested workspace/platform revisions.
- The current reference runtime target is isolated per client/environment and has no public VM IP; administration uses the supported private GCP path described in [`security.md`](security.md) and [`cloud-bootstrap.md`](cloud-bootstrap.md).
- Runtime/model, governed-action and usage/cost evidence use bounded attributable identifiers; they do not depend on private chain-of-thought.

## Detailed authorities

- Operator entry point: [`client-onboarding.md`](client-onboarding.md)
- Bootstrap and federation: [`cloud-bootstrap.md`](cloud-bootstrap.md)
- Credential boundaries: [`secrets.md`](secrets.md)
- Production hardening: [`security.md`](security.md)
- Governed actions/audit: [`governed-tools.md`](governed-tools.md)
- Capability and resource contracts: [`contracts.md`](contracts.md)
- Channel adapters and principals: [`channels.md`](channels.md)
- Usage/cost attribution: [`metering.md`](metering.md)
- Live acceptance evidence: [`live-proof.md`](live-proof.md)
- Enterprise assurance evidence: [`assurance.md`](assurance.md)

Expansion decisions such as additional clouds, Kubernetes, a control plane, Connector SDK, model gateway, dedicated policy engine or client portal are intentionally **not** decided in this implementation document. The [Confluence target architecture](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/11796484) requires registry-qualified, governed upstream integrations and customer-added MCP/API connections. The repository now implements part of #158–#159 (operator-assisted MCP/API execution and secret/OAuth enrollment), **not** a complete client self-service connector product or a proven live SaaS route. Keep the remaining acceptance and evidence gates in the Confluence Build Plan, Roadmap and [#159](https://github.com/kingyx3/model-garden/issues/159).
