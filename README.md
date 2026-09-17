# Model Garden

Model Garden is the implementation repository for a governed AI-employee platform and consulting delivery baseline. It contains the portable client contracts, compiler/runtime adapters, governed Tool boundary, deployment automation, tests/evals and enterprise evidence automation used by the supported release.

**Release source of truth:** [`VERSION`](VERSION) plus immutable [GitHub Releases](https://github.com/kingyx3/model-garden/releases). Client workspaces pin a published release in `platform.lock.yaml`; prose documentation is not a version registry.

## Documentation authority

Keep one owner per fact and link rather than copy.

| Authority | Owns |
| --- | --- |
| [Confluence — Model Garden Documentation](https://modelgarden.atlassian.net/wiki/spaces/BA/overview) | Business/product architecture, build-vs-reuse decisions, roadmap/evidence gates, commercial and operating policy, client-facing process and founder/operator checklists. |
| This repository | Executable implementation, release/version, code-specific architecture, exact commands/workflows, Terraform, tests/evals, provider mechanics and engineering evidence. |

Repository implementation docs:

- [`docs/architecture.md`](docs/architecture.md) — current implementation boundaries and deployment contract.
- [`docs/client-onboarding.md`](docs/client-onboarding.md) — supported operator entry point and unavoidable inputs.
- [`docs/cloud-bootstrap.md`](docs/cloud-bootstrap.md) — keyless GCP bootstrap mechanics.
- [`docs/secrets.md`](docs/secrets.md) — credential ownership, storage and authorization mechanics.
- [`docs/security.md`](docs/security.md) — production hardening for the supported deployment.
- [`docs/governed-tools.md`](docs/governed-tools.md) — Tool/MCP authority, approvals and action audit.
- [`docs/metering.md`](docs/metering.md) — canonical usage/cost attribution and Lago integration.
- [`docs/live-proof.md`](docs/live-proof.md) — executable live-proof/evidence collection.
- [`docs/assurance.md`](docs/assurance.md) — DDQ/evidence registry and CI assurance pack.
- [`docs/self-hosting.md`](docs/self-hosting.md) — time-sensitive hosting/provider implementation facts.
- [`docs/contracts.md`](docs/contracts.md) — portable resource contracts.
- [`docs/releasing.md`](docs/releasing.md) — release mechanics.

If a repository document starts defining pricing, service policy, roadmap priority or client engagement process, move that decision to Confluence and link it. If Confluence starts copying commands, release numbers, workflow internals or provider-specific implementation facts, link back here instead.

## Supported implementation

```text
private client workspace
Agent + Skills + Tools + Knowledge + evals
                |
        validate / compile
                v
       Hermes runtime profile
                |
       governed MCP Tools
                |
     isolated client runtime

bootstrap once -> remote Terraform state -> GitHub OIDC
normal deploy  -> Terraform + Model Garden runtime convergence
```

The current reference deployment is keyless GCP plus an isolated Docker host. Exact boundaries and supported seams live in [`docs/architecture.md`](docs/architecture.md); business/product rationale and future expansion decisions live in Confluence.

## Launch a client

Use a **published** Model Garden release with authenticated `gh`, `git`, Terraform and preferably `gcloud`:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

`scripts/launch.py` is the normal managed-client entry point. See [`docs/client-onboarding.md`](docs/client-onboarding.md) for inputs, overrides and the DEV-to-PROD flow; see [`docs/cloud-bootstrap.md`](docs/cloud-bootstrap.md) for bootstrap internals and [`docs/secrets.md`](docs/secrets.md) for credential boundaries.

## Repository layout

```text
assurance/                    enterprise DDQ/evidence registry and mappings
contracts/v1/                 versioned portable resource contracts
examples/workspace/           reference client desired state and evals
platform/connectors/          thin business-system adapters
platform/channels/            replaceable channel adapters
platform/runtime/             governed runtime/MCP boundary
platform/metering/            external metering adapters
scripts/                       launch, compile, govern, assure and deploy helpers
tests/                         regression and acceptance coverage
infra/bootstrap/gcp/           keyless trust/state bootstrap
infra/docker-host/gcp/         isolated GCP Docker-host desired state
templates/client-workflows/    generated client deployment workflow
```

## Validate and assure

```bash
python3 -m pip install -r requirements-dev.txt
make validate
make assurance
make assurance-scan
```

CI validates the current Terraform, contracts/compiler/runtime/operator paths, reference evals, pinned Hermes compatibility and commit-scoped enterprise evidence. Evidence automation reports what the repository can prove; it does not manufacture certifications, penetration-test results, provider terms or deployment-specific live proof.

For current stage, priorities and evidence gates, use the [Model Garden Roadmap — MVP to Ideal State](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/950302/Model+Garden+Roadmap+MVP+to+Ideal+State) rather than adding status prose here.
