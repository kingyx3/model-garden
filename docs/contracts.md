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

All of this is additive to `modelgarden.ai/v1`: existing workspaces without declarations validate and compile unchanged.

Stable operational contract:

- `usage-attribution.schema.json` — canonical non-secret client/Agent/environment/provider and optional cost/trace/subscription/shared-pool dimensions used to correlate runtime consumption with showback and billing. This is platform-generated operational metadata, not client-authored Agent/Skill desired state.

Business teams should depend on the business resource contracts rather than infrastructure details or physical model IDs. Runtime/commercial integrations should depend on the explicit operational contracts rather than inventing provider-specific attribution shapes. Breaking schema changes require a new API version, e.g. `modelgarden.ai/v2`.

## Validate a workspace

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/validate-workspace.py examples/workspace
```

The examples are intentionally small and should become the seed for a separate customer workspace template later.
