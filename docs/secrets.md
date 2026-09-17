# Secrets and credentials

Model Garden separates **one-time cloud bootstrap authority** from **runtime/integration credentials**. Neither belongs in client business-authored Agent, Skill or Knowledge content.

This page is the repository authority for **who owns which credential/account, where raw values live, and how runtime authority is separated from business configuration**. Confluence may summarize the business operating rule, but exact implementation/storage mechanics should link here instead of being copied.

## Ownership rule

**Tool/connector code and credentials are separate concerns.** Model Garden owns reusable Tool/connector implementations. The credential or authorization used by a Tool should belong to the party that owns the target account, data or commercial provider relationship.

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
calendar:
  connector: google-calendar
  credential_ref: secret://google/dev-calendar
```

The actual manually maintained runtime values are stored in the protected GitHub `dev` and `prod` Environments as the aggregate secret:

| Secret | Purpose |
|---|---|
| `MODEL_GARDEN_RUNTIME_SECRETS_JSON` | JSON object whose keys exactly match the `secret://` references required by that environment. Missing or extra keys fail closed. |

OAuth refresh tokens or other dynamic grants created by an end-user/provider authorization flow may be stored by the governed runtime/provider authorization store where appropriate; they are not business-authored workspace content.

The deployment process resolves the aggregate map, removes it before Docker execution and injects only the credentials required by the selected runtime processes/capabilities. Hermes/model execution must not receive unrelated Calendar, CRM or telephony credentials; governed Tool sidecars receive only their connector-specific credentials.

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
- Add another cloud/provider path only from real deployment evidence; preserve the same externalized-secret, least-privilege and short-lived-identity invariants.
