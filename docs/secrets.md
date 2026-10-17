# Secrets and credentials

Model Garden separates **one-time cloud bootstrap authority** from **runtime/integration credentials**. Neither belongs in client business-authored Agent, Skill or Knowledge content.

This page is the repository authority for **who owns which credential/account, where raw values live, and how runtime authority is separated from business configuration**. Confluence may summarize the business operating rule, but exact implementation/storage mechanics should link here instead of being copied.

## Ownership rule

**Governance and upstream integration credentials are separate concerns.** Model Garden owns the portable Tool contracts and governed gateway, not bespoke vendor clients. The credential or authorization used by a Tool should belong to the party that owns the target account, data or commercial provider relationship.

| Credential / account | Default for managed SMB client | Use client-owned instead when… | Reference live proof |
|---|---|---|---|
| GitHub operator identity + client workspace repo | Model Garden operator/private repo | Client requires its repo in its own GitHub org/control boundary | Model Garden |
| GCP target project/account | Model Garden-owned isolated project/environment | Contract, client IT/security policy or billing requires client-owned cloud | Model Garden sandbox/reference project |
| Temporary GCP bootstrap service-account JSON | Supplied by the owner/admin of the target GCP project; used once then revoked/deleted | Always follows target-project ownership; never a long-lived runtime secret | Model Garden-created temporary bootstrap credential |
| Terraform state, WIF/OIDC, deploy + runtime service accounts | Provisioned automatically inside the target project; ownership follows that project | Client requires named identities/policies under its cloud controls | Model Garden reference project |
| Model/provider API credential | Model Garden-managed provider account when usage is bundled into the managed service | Client requires direct provider billing, enterprise terms, data controls or quota | Model Garden/consultant provider credential |
| Google Workspace / Microsoft 365 / CRM / calendar / booking authorization | Client-owned account authorization with least privilege; Model Garden performs technical setup | Production business-system access is inherently client/system-owner authorized | Model Garden-controlled test tenant/account only |
| Voice / LiveKit / SIP / telephony provider credential | Model Garden-managed provider account is acceptable for the managed service | Client already owns the number/PBX/SIP trunk, needs direct billing or requires provider ownership | Model Garden/consultant test provider, number and SIP path |
| Business rules, authoritative documents and production approvals | Client-owned | Never replaced by Model Garden credentials or unilateral approval | Synthetic demo data only |

For the **reference live proof**, use Model Garden/consultant-owned sandbox cloud, model and voice credentials plus controlled test business-system accounts. The proof should not depend on a client. A real client rollout then replaces the relevant authorizations with client-owned production accounts wherever the client owns the underlying business system, number/PBX, cloud account, provider contract or billing relationship.

## Preferred managed GCP bootstrap

The normal bootstrap command is:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

The service-account JSON is a **temporary local bootstrap credential**. It is used only to establish:

- remote Terraform state;
- GitHub Workload Identity Federation restricted to the exact client repository;
- a least-privilege deployment identity;
- a separate runtime identity; and
- the non-secret repository variables required by the generated keyless deployment workflow.

The bootstrap credential follows ownership of the target GCP project: Model Garden creates it for a Model Garden-owned project; a client cloud admin creates or authorizes it for a client-owned project.

The JSON must never be committed to Git, copied into the client workspace, stored as a long-lived GitHub secret, embedded in workflow source, passed as a Terraform variable or intentionally persisted in Terraform state.

After the first keyless GitHub OIDC DEV deployment succeeds, the launch flow records verification. When local `gcloud` is available it revokes the exact bootstrap key and deletes the local JSON by default. Otherwise it prints one explicit revocation command and leaves the file in place until revocation succeeds.

Normal DEV/PROD deployment must continue without that bootstrap credential. Exact bootstrap mechanics live in [`cloud-bootstrap.md`](cloud-bootstrap.md).

## Runtime and integration credentials

Application credentials remain separate from cloud bootstrap identity. Examples include model-provider API credentials, Google/Microsoft authorization, LiveKit/telephony credentials, and CRM/email/other selected connector credentials.

Client environment files contain only logical references, for example:

```yaml
environment: dev
runtime:
  profile: receptionist
  model_credential_ref: secret://model/dev
connectors:
  kb: {provider: text-knowledge, tier: 0}
  crm: {provider: built-in-crm, tier: 0}
```

The actual manually maintained runtime values are stored in the protected GitHub `dev` and `prod` Environments as the aggregate secret:

| Secret | Purpose |
|---|---|
| `MODEL_GARDEN_RUNTIME_SECRETS_JSON` | JSON object whose keys exactly match the `secret://` references required by that environment. Missing or extra keys fail closed. |

OAuth refresh tokens or other dynamic grants created by an end-user/provider authorization flow may be stored by an independently reviewed governed runtime/provider authorization store where appropriate; they are never business-authored workspace content. **Model Garden v0.8.0 does not ship native Google/Microsoft Calendar or other SaaS adapter tokens/refresh flows.** Approved upstream infrastructure must own provider OAuth and rotation; the governed boundary must enforce each coworker's revocable identity, approved operations and audit before unattended client integrations. See [`governed-tools.md`](governed-tools.md).

The deployment process resolves the aggregate map, removes it before Docker execution and injects only the credentials required by the selected runtime processes/capabilities. Hermes/model execution must not receive unrelated Calendar, CRM or telephony credentials; governed Tool sidecars receive only their connector-specific credentials.

## Owner-entered credentials (sealed vault)

A credential a client owner adds later (an API key or token for a custom API or MCP server, #159) does not go through the deploy-time map and is never typed into chat, a workspace file or an issue.

1. Generate a vault key pair once per environment (`python3 -c "import importlib.util as u;s=u.spec_from_file_location('v','platform/core/sealed_vault.py');m=u.module_from_spec(s);s.loader.exec_module(m);print(m.generate_keypair())"`). Put the **private** key in the environment's secret map and reference it as `upstream.mcp.vault_key_ref`; it is injected into the governed sidecar alone. Put the **public** key in `gateway.enrollment.vault_public_key` (it is not secret). Enrollment needs the gateway's web sign-in and `vault_key_ref`; `secret_refs` may then be empty when every upstream credential is owner-entered. Deploy refuses a public key that does not match the private key behind `vault_key_ref`. Credentials are one line of printable ASCII, because they travel in HTTP headers.
2. The operator opens an enrollment for one coworker's connection: `runtime-connection.py /opt/data/.modelgarden credential open --agent <agent> --connection <id> --kind bearer|api-key|basic|oauth-client --owner <owner address> --actor <operator> --origin <gateway origin>`. It prints a single-use link (15 minutes by default) and the stable reference to use in the coworker's grant and the connection declaration (`secret://<client>-<environment>/<agent>/<connection>`).
3. The owner opens the link, signs in with the address the enrollment names (a magic link that returns to the page) and pastes the value. Anyone else's session, an expired or used link, or no session is refused. The gateway seals the value to the sidecar's public key on the shared governance volume; it cannot read it back. The ledger records who set which version, never the value or the link.
4. The sidecar resolves the stable reference from the vault first, then from deploy-time variables. Rotating is another `credential open`; the stable reference moves to the new version and the old one is deleted, with no restaging. `credential revoke` deletes it, and the reference stops resolving at once (it never falls back to an older version). `credential status` shows only whether it is set, its version, who set it and a masked length.

The vault is files on the governance volume, encrypted per value (X25519, HKDF-SHA256, ChaCha20-Poly1305, the reference bound as associated data), 0600 in a 0700 directory. Back the volume up with the rest of governance data; losing the private key makes every owner-entered credential unrecoverable, and the owner re-enters it.

## GitHub/cloud identity after bootstrap

The generated keyless client workflow uses non-secret repository variables for identifiers such as GCP project, region, Workload Identity Provider, deployment service account and Terraform state location. GitHub exchanges its OIDC identity for short-lived cloud credentials at deployment time.

Do not create a permanent cloud JSON key merely to make routine deployment easier.

## Client-owned cloud

Client-owned and Model Garden-owned cloud use the same contract. Ownership changes who controls the cloud account, billing and break-glass administration; it does not change the client workspace, runtime model or keyless deployment mechanics.

## Self-hosted-runner fallback

The private client-labelled self-hosted GitHub runner path remains a compatibility/exception option when cloud-native remote administration is unavailable or prohibited.

When deliberately selected:

| Variable / secret | Purpose |
|---|---|
| `MODEL_GARDEN_DOCKER_RUNNER` | Client-specific private self-hosted runner label. |
| `MODEL_GARDEN_RUNTIME_SECRETS_JSON` | The same environment-scoped runtime secret map described above. |

Do not provision a self-hosted runner for the preferred managed GCP path merely because the fallback exists.

## Rules that always apply

- Never commit API keys, OAuth tokens, service-account JSON or runtime secret maps.
- DEV and PROD use separate environment-scoped credentials.
- Store only the minimum credential required by the selected capability.
- Prompts, Skills and Agent instructions cannot create or expand authority.
- Record owner, purpose and revocation path for every production integration.
- Rotate provider/runtime credentials independently of Terraform state and client business definitions.
- Provider OAuth and refresh-secret custody belong to approved upstream integrations or a vetted credential store, not bespoke Model Garden vendor adapters; owner consent, scoped access, revocation, expiry alerting and live proof remain open under [#66](https://github.com/kingyx3/model-garden/issues/66), #86 and #159.
- Add another cloud/provider path only from real deployment evidence; preserve the same externalized-secret, least-privilege and short-lived-identity invariants.
