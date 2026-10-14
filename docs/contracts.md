# Platform contracts

`contracts/v1/` is the compatibility boundary between the Model Garden platform, business-owned AI content and stable operational integrations.

Supported v1 business resources:

- `Agent` — composition of model profile, instructions, skills, tools and knowledge.
- `Skill` — reusable reasoning/instruction package.
- `ModelProfile` — logical model requirement mapped to approved candidates.
- `Tool` — executable enterprise capability with an explicit risk class.
- `KnowledgeSource` — approved source and data classification.
- `Policy` — portable policy metadata/rules; runtime enforcement can later be backed by OPA or another engine.

## Capability contracts

`contracts/v1/capabilities.json` (validated by `capability-catalog.schema.json`) is the platform-owned catalog of provider-neutral Tool families: `chat`, `mail`, `calendar`, `files`, `docs`, `tickets`, `crm`, `code` and `kb`. Each portable Tool (for example `mail.send`, `tickets.create`) has argument and result schemas, a risk class (`read`, `write`, `privileged`, `external-communication`) and the argument fields that are safe to keep verbatim in the action ledger.

- `Agent` and `Skill` resources may declare `requiredCapabilities` and `optionalCapabilities`, naming a family (`calendar`) or a portable Tool (`calendar.book`). A required declaration on any selected Skill makes the capability required for the Agent.
- A `Tool` may opt in with `spec.capability: <family>`; its name must then be a portable Tool of that family and its risk must match the catalog. The reference `calendar.availability`, `calendar.book` and `crm.read` Tools do this with no behaviour change.
- Only the EnvironmentBinding chooses a provider, as `connectors.<family>.provider` (the legacy `connectors.google_calendar` / `microsoft_calendar` shapes map to `calendar`). Optional non-secret `tier` (0–4, default 1), `scopes` and `owner` describe the connection.
- `compile-workspace.py --environment-binding <file>` resolves declarations: a required capability with no provider fails closed with a message naming the Agent, capability and environment; an unbound optional one compiles and is reported under `capabilities.degraded` with its fallback tier (Tier 4, human hand-off, until built-in Tier 0 storage exists). Docker deployment re-runs the same check against the real binding.
- The fail-closed message also names the Skill(s) that require each missing capability. With a binding, the compiled output carries `capabilities.skills`: one entry per selected Skill (plus the Agent when it declares capabilities itself) with `ready` and each dependency as `bound` (provider, tier), `missing` or `degraded` (fallback tier). It copies no token references, calendar IDs, scopes or owners.
- `compile-workspace.py --environment-binding <file> --readiness` prints that per-Skill table instead of compiling and exits non-zero if any Skill is blocked.

All of this is additive to `modelgarden.ai/v1`: existing workspaces without declarations validate and compile unchanged.

Stable operational contract:

- `usage-attribution.schema.json` — canonical non-secret client/Agent/environment/provider and optional cost/trace/subscription/shared-pool dimensions used to correlate runtime consumption with showback and billing. This is platform-generated operational metadata, not client-authored Agent/Skill desired state.

Business teams should depend on the business resource contracts rather than infrastructure details or physical model IDs. Runtime/commercial integrations should depend on the explicit operational contracts rather than inventing provider-specific attribution shapes. Breaking schema changes require a new API version, e.g. `modelgarden.ai/v2`.

## Integration registry and readiness-to-connect boundary

`contracts/v1/integrations.json` is the **single technical inventory** of providers by capability family, connection method/tier, maturity, portable Tools, tracking issue and (for implemented providers) adapter source. Its schema, Tool coverage, adapter existence and live-proof requirements are exercised in tests. **`built-credential-free` means an adapter and automated tests exist; it does not mean a real client tenant, refreshable authorization, or production acceptance has been proven.** No provider is currently marked `live`.

Render the provider directory for review and Confluence sync without maintaining a second manually authored list:

```bash
python3 scripts/integration-directory.py --format markdown
python3 scripts/integration-directory.py --family crm --format json
```

An operator can inspect a family, then request an **explicit, non-secret scope preview** before seeking the client owner's authorization:

```bash
python3 scripts/client-operator.py connect calendar
python3 scripts/client-operator.py connect calendar --provider google-calendar \
  --tools calendar.availability --owner client-owner@example.com --operator consultant@example.com
python3 scripts/compile-workspace.py examples/workspace --agent receptionist \
  --environment-binding environments/dev.yaml --readiness
```

The CLI does **not** obtain OAuth tokens, provision a tenant, save a secret, test a live provider or change an EnvironmentBinding. Planned providers fail the preview. An implemented provider returns `readyToConnect: false` with the remaining live consent/health gate, rather than implying connectivity. The normal private GitHub Environment secret map and reviewed PR → DEV → PROD workflow remain the actual deployment interface. The Skill readiness report links missing capability families back to the provider options; the coworker's `platform.capabilities` manifest also includes selected per-Skill readiness. Full guided provisioning and real OAuth lifecycle remain tracked under [#110](https://github.com/kingyx3/model-garden/issues/110) and [#86](https://github.com/kingyx3/model-garden/issues/86).

## Shipped-provider activation gate

An EnvironmentBinding naming a provider is not evidence that its connector exists. The
compiler, `client-operator skills` readiness report, non-secret coworker
`platform.capabilities` manifest and Docker deployment now consult
`contracts/v1/integrations.json` to determine **implemented** support. Only
`built-credential-free` or `live` providers with a matching family, correct tier
and a real local adapter satisfy a dependency. Historical `google_calendar`,
`microsoft_calendar`, `google` and `microsoft` aliases continue to work.

A planned HubSpot/Jira/Zoho/SharePoint/Drive provider, an unregistered provider or
a forged Tier 0 `built-in-crm`/`text-knowledge` binding **cannot activate a Skill**.
Required capabilities remain blocked, optional ones degrade to the declared
fallback, and production deployment repeats the check even when it receives a
previously compiled desired-state document. A missing or invalid registry
fails closed. This gate verifies shipped adapter presence, **not** client
consent, a current access token, live provider health or completed acceptance
proof. Real OAuth and live proof are still tracked by
[#66](https://github.com/kingyx3/model-garden/issues/66) and
[#86](https://github.com/kingyx3/model-garden/issues/86).

## Enable or disable individual Skills safely

An Agent may list a Skill in `spec.skills` while keeping it off using `spec.disabledSkills`. Example:

```yaml
spec:
  skills: [answer-faq, take-message, book-appointment]
  disabledSkills: [book-appointment]
```

Disabled Skills remain in the non-secret readiness report but are **not** materialized into the Hermes runtime, do not contribute required capabilities, and suppress Tools used exclusively by disabled Skills. Tools shared with enabled Skills remain selected. Removing a Skill from `disabledSkills` is an activation: the ordinary required-capability validation then **fails closed** if its provider is missing. Keep direct Agent instructions aligned with what is enabled; disabling a Skill is not a grant/revocation of an underlying client provider credential.

```bash
python3 scripts/client-operator.py skills <private-client-workspace> --environment dev --agent receptionist
```

This prints `ready`, `DISABLED` and `BLOCKED`, dependencies, and registry-backed `connect <family>` guidance without exposing tokens. An intentionally disabled Skill does not block the current deployment; enabling it without its required capability does. The `platform.capabilities` coworker response includes the same per-Skill `enabled`, `ready`, `canEnable` and dependency facts. When a connector is failed or revoked, live runtime health overlays mark dependent required Skills unavailable until a named operator reauthorizes the connection.

The receptionist's complete Tier 0 `kb` / `crm` support and interaction logging remain **unimplemented** (#77, #108), as does full consent/provisioning (#110). Do not represent these capability checks as real external provider proof.

## Validate a workspace

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/validate-workspace.py examples/workspace
```

The examples are intentionally small and should become the seed for a separate customer workspace template later.
