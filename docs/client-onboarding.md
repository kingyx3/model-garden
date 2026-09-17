# Client onboarding

This page owns the **repository-side operator entry point** for a normal Model Garden client. It deliberately does not repeat cloud-bootstrap internals, secret-storage rules, billing integration details or business/commercial handoff steps.

- Confluence owns the engagement lifecycle and repeatable founder/operator checklist.
- [`cloud-bootstrap.md`](cloud-bootstrap.md) owns the keyless infrastructure bootstrap contract.
- [`secrets.md`](secrets.md) owns credential/account ownership and secret handling.
- [`architecture.md`](architecture.md) owns implementation boundaries.

Model Garden platform code stays in this repository. Create one separate private workspace repository per client and keep it limited to client desired state, version pins, environment configuration and evals. Do not fork Model Garden per client.

## Standard managed path

For early SMB clients, use one resumable command from a **published** Model Garden release:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

`scripts/launch.py` is the public operator entry point. By default it derives the target GCP project from the temporary JSON's `project_id`, derives `<authenticated-github-user>/acme-ai-workspace`, and uses `--ownership model-garden`.

Override only a target that genuinely differs:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json \
  --github-repo client-org/acme-ai-workspace \
  --gcp-project client-target-project \
  --ownership client
```

Ownership changes who controls the account/project and break-glass administration; it does not change the workspace/runtime contract.

## What the launch flow owns

The standard launch flow must derive or automate repeatable technical work rather than turn it into a per-client checklist. It creates/reuses the private client workspace and repository, configures protected DEV/PROD runtime secret maps, establishes keyless GCP trust/state from one temporary local credential, installs the keyless client workflow, reconciles and verifies DEV, records bootstrap verification, revokes/deletes the temporary key where local tooling permits, and runs environment doctor checks.

The flow is resumable. Reruns reuse completed workspace, secret, cloud and deployment state and fail closed if the existing client/repository/cloud identity conflicts with the requested target.

Exact bootstrap resources, Terraform ownership and GitHub OIDC mechanics live in [`cloud-bootstrap.md`](cloud-bootstrap.md), not here.

## Unavoidable operator/client inputs

Only information Model Garden cannot safely invent or derive should require input:

- client slug and the bounded employee/workflow being deployed;
- one temporary bootstrap service-account JSON for the target GCP project on first bootstrap;
- a repository/project/ownership override only when the inferred default is not the intended target;
- runtime/integration credentials actually referenced by the selected environment;
- client-owned OAuth/account consent for client business systems;
- business-approved Agent/Skill/Knowledge/eval content and human handoff rules;
- production business approval before live traffic.

Credential ownership follows the target account/data/commercial relationship. For the managed no-IT path, Model Garden may own the cloud/model/voice provider account; production authorization into Google Workspace, Microsoft 365, CRM, calendar, booking or another client business system remains client-authorized. See [`secrets.md`](secrets.md).

## Business configuration is separate from deployment

The one-command bootstrap removes technical choreography; it does not guess business behaviour. Configure the initial Agent's role/instructions, selected curated Skills, approved Tools, authoritative Knowledge references, human handoff rules and representative evals from discovery.

Receptionist is the reference wedge, not a mandatory platform shape. A different bounded role normally changes only the client workspace:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json \
  --agent operations-coordinator \
  --role-title "Operations Coordinator"
```

Change Model Garden core only when a reusable Skill, connector, governance capability or runtime seam is genuinely missing.

## Normal delivery after bootstrap

Once bootstrap is verified, GitHub is the engineering control plane:

```text
pull request
  -> validate only

merge to dev
  -> keyless DEV deploy
  -> evals / pilot / approval

promote tested revision to main
  -> keyless PROD deploy
  -> production smoke / acceptance
```

No long-lived cloud deployment JSON is needed after bootstrap. Platform releases do not silently mutate clients. Upgrade a client by changing `platform.lock.yaml` in a pull request, validating/evaluating in DEV, then promoting the same tested revision to PROD.

## Lower-level paths are implementation details

`scripts/launch.py` delegates to `scripts/launch-client.py`, which composes `scripts/client-operator.py` and `scripts/bootstrap-cloud.py`. Run those lower-level commands directly only for diagnostics, automation internals or an intentional non-default environment.

The private self-hosted Docker-runner path remains a compatibility/exception option where cloud-native remote administration is unavailable or prohibited; it is not the preferred managed GCP path.

## Client/platform ownership boundary

Platform-owned here: contracts, compiler, Hermes adapter, governed actions, reusable Skills/connectors/channel adapters, keyless cloud bootstrap, Docker target adapter, thin Lago usage adapter, operator helpers and shared CI.

Client-specific desired state in the separate private workspace: `modelgarden.yaml`, `platform.lock.yaml`, `agents/`, genuinely client-specific `skills/`, Knowledge references, `evals/`, and environment-specific non-secret configuration.

For the business-side lifecycle, deployment checklist and production handover, use the canonical Confluence Model Garden documentation rather than recreating those procedures here.
