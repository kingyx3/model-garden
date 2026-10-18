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
          business-system adapters

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
| Business actions | Governed MCP Tool boundary with explicit Tool selection, environment-scoped connector credentials, `allow | approval | deny`, exact-action approval and a tamper-evident schema-v3 action ledger with read events. Per-coworker provider authentication is not shipped (see #129). |
| Business-system integration | Provider-neutral capability catalog (`contracts/v1/capabilities.json`) with required/optional declarations and a per-environment capability manifest. Calendar and Tier 0 CRM/text Knowledge are credential-free tested. Mail (Gmail/Outlook), CRM (HubSpot/Salesforce) and Google Drive have additional injected-transport adapters and tests but are **in-development and not served from the governed sidecar**. Consult the [canonical integration registry](../contracts/v1/integrations.json) and [provider evidence gate](contracts.md#provider-evidence-manifest-and-go-live-gate) for current implementation and go-live status; no third-party provider is production-live without valid tenant proof. |
| Channels | One text channel adapter contract with verified principal mapping (no text adapters shipped yet); LiveKit is the reference voice path with two-way transcript storage tested, but live-call, generated summary and CRM filing are unfinished (#107/#108). |
| Usage/cost attribution | Canonical non-secret attribution events emitted to the external Lago metering/billing seam. |
| Infrastructure | Terraform-managed keyless GCP bootstrap and isolated Compute Engine Docker host. |
| Delivery control | GitHub PR validation plus generated keyless environment deployment workflow. |
| Skills and coworker identities | Individual Skill selection, disablement, dependency readiness **and first-class reusable Skillsets** ([#128, completed](https://github.com/kingyx3/model-garden/issues/128)) are implemented. Multi-Agent bundle rendering (`scripts/deploy-team-docker.py`, one runtime and attested identity per Agent) and the task-graph executor with attenuated delegation, a shared budget and human-handoff nodes (`platform/core/task_graph.py`, `scripts/runtime-tasks.py`; see [contracts](contracts.md#multi-agent-task-graphs)) are implemented credential-free and unproven against a live client (#95, #96, #97, #98). Fully separate downstream provider identities for each coworker (#129) and client self-service Studio (#130) remain gated, unfinished work. |

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

Expansion decisions such as additional clouds, Kubernetes, a control plane, Connector SDK, model gateway, dedicated policy engine or client portal are intentionally **not** decided in this implementation document. The [Confluence target architecture](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/11796484) requires registry-qualified, governed upstream integrations and customer-added MCP/API connections; **#157–#159 remain target work, not a functioning customer-secret enrollment product**. Their evidence gates belong to the Confluence Build Plan and Roadmap; when approved, this document should change only after the implementation exists.
