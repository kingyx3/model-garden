# Model Garden

Model Garden is an OSS-first platform and consulting delivery baseline for deploying governed AI employees for small and mid-sized businesses without requiring a client IT team.

**Release source of truth:** [`VERSION`](VERSION) plus immutable GitHub Releases. Client workspaces pin a published Model Garden release in `platform.lock.yaml`; prose documentation should not become a competing version registry.

## Architecture in one screen

```text
shared versioned Model Garden platform
             |
             | pinned by platform.lock.yaml
             v
private client workspace
Agent + Skills + Tools + Knowledge + evals
             |
      validate / compile
             |
             v
Hermes + governed MCP Tools
             |
             v
isolated client Docker host

cloud bootstrap once
  -> Terraform remote state
  -> GitHub OIDC / workload federation
  -> deploy + runtime identities

normal operation
  -> GitHub Actions
  -> Terraform infrastructure convergence
  -> Model Garden runtime convergence
```

Core rules:

- one shared Model Garden platform repo; do not fork it per client;
- one separate private business workspace per client;
- Receptionist is the reference wedge, not a platform restriction;
- Git holds business/software desired state; Terraform holds infrastructure desired state;
- raw secrets never live in Agent/Skill/Knowledge/workspace business content;
- one temporary cloud bootstrap credential may establish trust, then normal deployment is keyless;
- Model Garden-owned and client-owned hosting use the same application/workspace contract;
- build only the Model Garden differentiation layer; prefer client systems, open standards and mature OSS for commodity capabilities;
- OSS-first does not mean self-host-at-all-costs: use the lowest-TCO replaceable option that preserves portability, isolation and governance;
- do not add Kubernetes, a control plane, Connector SDK, custom gateway or portal without evidence from live deployments.

## Documentation authority

Keep one authority per topic and link to it instead of copying the same operating detail across files or systems.

- **Confluence — Model Garden Documentation:** business/product architecture, roadmap, commercial/engagement process, founder/operator checklists and client-facing operating decisions.
- **This repository:** executable implementation contracts, code-specific deployment mechanics and engineering evidence.
- [`docs/architecture.md`](docs/architecture.md): implementation architecture and boundaries.
- [`docs/client-onboarding.md`](docs/client-onboarding.md): the normal operator entry point and only the unavoidable onboarding inputs.
- [`docs/cloud-bootstrap.md`](docs/cloud-bootstrap.md): keyless infrastructure bootstrap internals.
- [`docs/secrets.md`](docs/secrets.md): credential ownership, secret storage and authorization boundaries.
- [`docs/security.md`](docs/security.md): production hardening.
- [`docs/assurance.md`](docs/assurance.md): evidence-backed enterprise DDQ answering, commit-scoped evidence packs and Singapore assurance mapping.
- [`docs/self-hosting.md`](docs/self-hosting.md): hosting/TCO decisions and provider-specific implementation facts that may change over time.
- [`docs/governed-tools.md`](docs/governed-tools.md): governed Tool/MCP execution and approval semantics.
- [`docs/metering.md`](docs/metering.md): thin usage-event integration; Lago remains the external billing/metering system.

Confluence should reference repository implementation docs for exact commands/workflow internals rather than duplicate them. Repository docs should reference Confluence for business/engagement policy rather than re-state commercial process.

## Preferred client launch

Run from a published Model Garden release with authenticated `gh`, `git`, Terraform and ideally `gcloud`:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

`scripts/launch.py` is the public operator entry point for the standard managed GCP path. It derives the target project from the temporary JSON and the private repo as `<authenticated-github-user>/<client>-ai-workspace` where possible. Use explicit `--github-repo`, `--gcp-project` and `--ownership` overrides only when the target differs.

The resumable launch flow creates/reuses the workspace and private repo, protected DEV/PROD environments, required runtime secret maps, remote Terraform state, Workload Identity Federation, deploy/runtime identities and the keyless deployment workflow; triggers and verifies DEV; and revokes/deletes the temporary bootstrap key where local tooling permits.

Runtime credentials such as model, Calendar and telephony credentials remain separate capability-scoped secrets and are requested only when the selected environment needs them. Credential/account ownership follows the target account or commercial relationship; see [`docs/secrets.md`](docs/secrets.md).

Lower-level `scripts/launch-client.py`, `scripts/client-operator.py` and `scripts/bootstrap-cloud.py` commands exist for orchestration, diagnostics and exceptional targets. They are not separate onboarding authorities.

## Delivery lifecycle

```text
business change
  -> PR validation
  -> merge to dev: DEV deployment
  -> evals / pilot / approval
  -> promote tested revision to main
  -> PROD deployment
```

Client upgrades normally change only `platform.lock.yaml` to a newer published Model Garden release, then follow the same DEV-to-PROD promotion path.

## Governance

Enterprise actions are not granted by prompts. Selected Tools are exposed through the governed MCP boundary and evaluated as `allow | approval | deny`; approval-required actions bind to the exact material request, consume each human decision once, and produce audit evidence.

## Enterprise assurance

Common customer security/privacy/AI DDQs can be answered from the version-controlled assurance registry without inventing compliance claims:

```bash
python3 scripts/ddq.py answer "Do you use customer prompts to train models?"
python3 scripts/ddq.py readiness
```

Generate a commit-scoped customer/reviewer evidence snapshot from the same registry:

```bash
make assurance       # registry + evidence metadata
make assurance-scan  # adds dependency, code, IaC, secret and SBOM scan evidence
```

CI produces an `enterprise-evidence-<commit>` artifact for every validated revision. Scanner output, evidence freshness/ownership and unresolved assurance gaps remain visible; they do not silently turn certifications, penetration testing, privacy operations or client-specific facts into supported claims.

The assurance layer maps repository evidence to common DDQ questions and Singapore references such as the PDPA/PDPC guidance, IMDA's Model AI Governance Framework for Agentic AI and relevant CSA assurance schemes. Provider-, legal-entity-, certification- and sector-specific answers remain explicitly review-gated. See [`docs/assurance.md`](docs/assurance.md).

## OSS and hosting boundaries

- **Hermes**: reference Agent runtime.
- **LiveKit / existing PBX**: replaceable voice/media layer; managed or self-hosted according to measured TCO and client requirements.
- **Lago**: default external usage metering/billing system; Model Garden emits attributable usage events only.
- **Langfuse**: optional observability when existing telemetry is insufficient.
- **Cloud provider**: GCP is the first current keyless Docker-host reference; add other clouds only when demanded by real deployment evidence.
- **Cloudflare**: optional DNS/HTTP edge where useful; never a required authority layer or a substitute for realtime UDP/SIP transport.

Detailed and time-sensitive hosting/provider guidance lives only in [`docs/self-hosting.md`](docs/self-hosting.md).

## Repository layout

```text
assurance/                    DDQ catalog, evidence registry/metadata and Singapore assurance map
contracts/v1/                 Versioned portable resource contracts
examples/workspace/           Reference employees and resources
platform/connectors/          Thin business-system adapters
platform/channels/            Replaceable channel adapters
platform/runtime/             Governed runtime/MCP boundary
platform/metering/            Thin external metering adapters
scripts/                       Launch, compile, govern, assure and deploy helpers
tests/                         Regression/acceptance coverage
infra/bootstrap/gcp/           Current keyless trust/state bootstrap
infra/docker-host/gcp/         Current isolated GCP Docker-host desired state
templates/client-workflows/    Current generated client deployment workflow
```

Historical Kubernetes/LiteLLM and EKS/GKE/AKS prototypes were removed from the active repository after the Docker/keyless architecture became canonical. Git history and older immutable releases preserve them if future evidence ever justifies revisiting those approaches.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
make validate
```

Release CI gates the current GCP bootstrap/Docker-host Terraform, contracts/compiler/runtime/operator tests, Receptionist acceptance evals, exact pinned Hermes compatibility and successful generation of the enterprise evidence artifact. Validated unreleased `VERSION` values publish automatically after landing on `main`; existing tags are never moved.

The current product priority is not another infrastructure layer: it is proving the reference deployment live end to end with real cloud/model/Calendar/voice credentials and a stable human fallback path.
