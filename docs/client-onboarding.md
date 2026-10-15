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
- runtime/integration credentials actually referenced by the selected environment, plus provider authorization and a sustainable renewal/revocation path;
- client-owned OAuth/account consent for client business systems;
- business-approved Agent/Skill/Knowledge/eval content and human handoff rules;
- production business approval before live traffic.

Credential ownership follows the target account/data/commercial relationship. For the managed no-IT path, Model Garden may own the cloud/model/voice provider account; production authorization into Google Workspace, Microsoft 365, CRM, calendar, booking or another client business system remains client-authorized. See [`secrets.md`](secrets.md).

## Connect the client's business stack

- **Google Workspace client:** Model Garden configures the approved Google Calendar OAuth/integration and uses `connectors.calendar.provider: google`; the client authorizes the relevant Calendar account. No Google Cloud administration is demanded from an ordinary SMB business user by default.
- **Microsoft 365 client:** Model Garden configures the approved Entra/Graph Calendar integration and uses `connectors.calendar.provider: microsoft` with an explicit mailbox. The client supplies the relevant account authorization and an Entra administrator participates only if its consent/policy requires it.
- Both provider paths expose the *same* `calendar.availability` / `calendar.book` Agent Tools, approvals, audit and client workspace lifecycle. Provider credentials never live in Agent/Skill definitions.
- **Do not equate a configured short-lived OAuth access token with production onboarding completion.** Refresh/revocation, delegated vs application permission scope, account ownership and live connectivity must be proven for the selected client before autonomous use. Microsoft Outlook mail/Teams/SharePoint and Google Gmail/Drive are not yet built-in governed connectors.

The precise supported calendar binding and current credential limitations live in [`governed-tools.md`](governed-tools.md). Broader business/provider intent is owned by the Google Workspace and Microsoft 365 Confluence integration guides.

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

## Business-owner Skill selection and activation gate

Before activating or modifying a Skill, run `python3 scripts/client-operator.py skills <private-client-workspace> --environment dev --agent <name>`. Show the owner the dependency state and next `connect <family>` step. Keep a Skill listed in the Agent's `spec.skills` but inactive with `spec.disabledSkills: [skill-name]` while its permission or data dependency is missing. Re-enable only after connection and explicit DEV validation; it cannot compile with an unbound required capability. The coworker's capability manifest reflects this state, including a runtime failure/revocation of a previously working connection. A shared `Skillset` is currently a **target catalogue package**, not an available workspace resource: use individual Skills and `disabledSkills` until [#128](https://github.com/kingyx3/model-garden/issues/128) is implemented. No nontechnical Agent Studio is shipped ([#130](https://github.com/kingyx3/model-garden/issues/130)).

Before enabling a Skill that requires a provider, verify the integration's status
in the canonical registry. Merely listing `connectors.crm.provider: hubspot`
or `connectors.kb.provider: confluence-knowledge` is not sufficient until the adapter
is implemented; the readiness and deployment gates reject such claims. The Tier 0
`connectors.kb: {provider: text-knowledge, tier: 0}` and
`connectors.crm: {provider: built-in-crm, tier: 0}` bindings are shipped and need no
credentials: an approved business-rules file (below) and a person who reviews the
contact log (`client-operator.py contacts review`; it prints personal data, so run it on
the client host and agree who may).
The reference receptionist now depends on these two Tier 0 bindings; a client with no
calendar keeps `book-appointment` under `disabledSkills` and needs nothing else (see
[contracts](contracts.md#reference-receptionist-on-a-zero-stack-client)).
Clients can continue to list those Skills under `disabledSkills` until their
dependencies are implemented and authorized. A shipped Calendar adapter still
needs actual account consent, working token rotation and live acceptance.

### Hand-offs and business rules

Name who performs actions the coworker cannot (set `MODEL_GARDEN_HANDOFF_ASSIGNEE` for the governed sidecar, or give a `human-only` policy rule its own `assignee`) and show them `python3 scripts/client-operator.py handoffs --project-name <env>`; they close each item with `handoff-done` or `handoff-decline --by <their identity>`. For a client with no knowledge system, have the owner write one `.md` or `.txt` of business rules, run `python3 scripts/knowledge-check.py rules.md --query "<a real customer question>"`, fix the warnings, and only then set the source's `status: approved`, select the `kb.search` Tool for the Agent and bind `connectors.kb` to `text-knowledge` at tier 0 (see [Tier 0 business-rules knowledge](contracts.md#tier-0-business-rules-knowledge)). `python3 scripts/client-operator.py connect kb --provider text-knowledge --tools kb.search --owner <owner> --operator <you>` prints the non-secret plan. No live deployment has exercised this path yet.

### Approval policy walkthrough

Run `python3 scripts/compile-workspace.py <private-client-workspace> --agent <name> --approvals` and walk the client owner through which actions run automatically, which need approval and which are denied for callers or senders who are not staff. Any change that relaxes an approval is an `allow` rule that needs the named client owner's sign-off digest before it compiles (see [Client approval policy](contracts.md#client-approval-policy)); record the owner's agreement outside the repository as well, and keep the sign-off lines under the client's `CODEOWNERS`.

## Client integration discovery and permission preflight

Use the canonical [integration registry and guide](contracts.md#integration-registry-and-readiness-to-connect-boundary) to show a client which CRM, mailbox, calendar and knowledge providers are implemented versus planned. `python3 scripts/client-operator.py connect <family>` lists provider choices by tier and maturity. Add `--provider`, `--tools`, `--owner` and `--operator` for a non-secret permission preview; it does not grant access or change credentials. For incomplete connectors and expired authorization, fail closed and keep live client provisioning manual until the corresponding implementation and real-provider proof gates are met. Do not create a second AI coworker against the same external service by copying the first one's grant. Current connection configuration is environment-scoped; independently approved, revocable **per-coworker** provider authentication is a prerequisite to safe multi-Agent autonomous operation ([#129](https://github.com/kingyx3/model-garden/issues/129), [#86](https://github.com/kingyx3/model-garden/issues/86)).

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
