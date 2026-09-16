# Architecture

Model Garden separates **portable client business desired state** from the **replaceable Agent runtime** and from **cloud infrastructure desired state**.

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

## Current deployment contract

- One shared, versioned Model Garden platform repository.
- One separate private workspace repository per client.
- GitHub is the engineering control plane: PRs validate, `dev` deploys DEV, `main` deploys PROD.
- A temporary local cloud bootstrap credential may establish trust and remote state; normal deployment must use short-lived federation afterwards.
- Terraform owns cloud infrastructure convergence.
- The Model Garden compiler, Hermes adapter and Docker deployer own application/runtime convergence.
- Raw secrets never belong in client business definitions or Terraform state.
- GCP is the first complete reference cloud path: Workload Identity Federation + isolated Compute Engine Docker host + IAP/OS Login.

## Current platform boundaries

- **Hermes**: reasoning loop, sessions, model execution and runtime Skill discovery.
- **Model Garden workspace/compiler**: portable business-facing Agent/Skill/Tool/Knowledge/eval contracts and deterministic resolution.
- **Governed MCP Tool path**: explicit selected Tools, scoped credentials, `allow | approval | deny`, exact-action approval and audit.
- **Channel adapters**: replaceable phone/web/other channel boundaries. LiveKit is the reference voice adapter.
- **Direct connectors**: narrow external-system capabilities required by proven workflows. Google Calendar is the current reference.
- **Deployment**: isolated Docker host with pinned runtime, health verification and rollback.
- **Metering**: thin attributable usage events to Lago; Model Garden does not implement billing.

## Deliberately not the default architecture

The repository retains earlier Kubernetes/multi-cloud reference code under `infra/aws`, `infra/gcp`, `infra/azure` and `platform/k8s`. These are **legacy/reference prototypes**, not the standard SMB deployment path and not release-blocking current architecture.

Do not introduce Kubernetes, a fleet control plane, a Connector SDK, a custom model gateway, a dedicated policy engine or a client portal until repeated live deployments justify the additional layer.

## Desired end state

Clients can change business-facing employee behaviour without runtime/cloud knowledge; Model Garden can upgrade runtimes/models/connectors without rewriting client definitions; hosting can move between Model Garden-owned and client-owned cloud without changing the business contract; and every material external action remains attributable, policy-controlled and regression-tested.
