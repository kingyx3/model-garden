# Production hardening

Model Garden's early-client deployment is intentionally small: one isolated client environment, one pinned runtime and the minimum cloud/application authority required by the selected workflows. Treat this as the production hardening baseline for the current Docker/keyless architecture.

## Identity and cloud access

1. Establish cloud trust with the one-time bootstrap credential, then verify GitHub OIDC / workload federation works without it.
2. Revoke the bootstrap key and delete the local JSON after keyless deployment is proven.
3. Keep deployment identity separate from runtime identity.
4. Grant the runtime VM/service account only the cloud permissions it actually needs.
5. Treat console/cloud-admin changes as break-glass operations; normal infrastructure changes converge through Terraform and GitHub.

## Runtime and secrets

1. Keep raw credentials outside Agent, Skill, Knowledge and environment YAML; those files contain only logical `secret://` references.
2. Store DEV and PROD credentials separately in protected GitHub Environments or the relevant provider authorization store.
3. Inject only the credential required by each runtime process or connector. Hermes/model execution must not receive unrelated Calendar, CRM or telephony credentials.
4. Pin the Model Garden release, Hermes revision, base image, voice runtime dependency and client workspace revision used in production.
5. Keep the Docker host isolated per early client/environment until evidence justifies a different tenancy model.

## Tool authority and business actions

1. Enterprise action authority lives at the governed Tool/MCP boundary, not in prompts.
2. Expose only Tools explicitly selected by the Agent/workspace.
3. Enforce `allow | approval | deny` before provider execution.
4. Bind approvals to the exact material action/arguments and consume each approval once.
5. Audit material actions with client/tenant, Agent, initiating identity where available, Tool/action, result, approval metadata and timestamp.
6. Governed-action audit records use a locked, fsynced schema-v2 hash chain so record modification/reordering/insertion or deletion inside the retained chain is detectable. Treat the local chain as tamper-evident rather than immutable; export it to a separately administered log/SIEM/object store where independent retention or non-repudiation is required.
7. Connector credentials must be least-privilege and independently revocable.

## Network and host hardening

1. The current GCP reference VM has no public IP. Administration uses IAP/OS Login and outbound internet access uses Cloud NAT.
2. The isolated subnet enables Private Google Access and VPC Flow Logs; Cloud NAT error logging is enabled for outbound troubleshooting/evidence.
3. The runtime VM enables Shielded VM Secure Boot, vTPM and integrity monitoring.
4. Do not expose Hermes or governed Tool services publicly unless the selected channel/integration contract explicitly requires it. The governed Tool service may bind inside the private Docker network so the Hermes container can reach it; no host port is published by the baseline.
5. Keep host firewall rules minimal and scope provider/channel ingress narrowly.
6. Apply regular OS/container dependency updates through a tested Model Garden release rather than mutating production hosts ad hoc.
7. Treat the current Debian-image-family and Docker-package installation path as a rebuild dependency: before regulated/high-assurance production, either pin/attest the host image and package set or retain equivalent rebuild evidence proving the replacement host consumed the approved versions. Do not claim bit-for-bit host reproducibility while those sources remain provider/package-repository resolved.

## Secure software supply chain

- Third-party GitHub Actions in the validation workflow are referenced by immutable commit SHA rather than mutable major-version tags.
- Runtime-critical Python dependencies use exact pins where they are deployed into the production bundle; automated weekly dependency update checks cover Python, GitHub Actions and supported Terraform modules.
- CI runs dependency vulnerability, Python security, Terraform/IaC, repository-secret and SBOM scans inside a branch-ruleset-required validation job. Scanner failure therefore fails that required check rather than producing advisory-only evidence.
- Enterprise Evidence Pack artifacts are retained for 90 days by CI; customer/contracts may require longer external retention.
- Dependency automation does not authorize an upgrade: every change still follows the normal version contract, tests/evals and DEV-to-PROD promotion path.
- See `SECURITY.md` for vulnerability disclosure and triage scope.

## Repository governance

The repository ruleset must be treated as part of the enterprise control plane, not merely developer convenience. At minimum it should require the validation contexts that cover release/version, Terraform, runtime/evals/Hermes compatibility and assurance scanning. For a multi-person production team, require independent approval for protected-branch changes, resolve review threads, use CODEOWNERS where ownership is meaningful, and restrict administrative bypass to documented break-glass use.

The current software can enforce CI content but cannot itself prove that a GitHub administrator never bypassed repository policy. Repository-setting evidence and audit logs remain part of deployment/operating assurance.

## Terraform state

The current GCP bootstrap under `infra/bootstrap/gcp` creates the remote Terraform state used by normal keyless deployment. Limit state access to the deployment/admin identities that require it. State can contain infrastructure metadata and may become sensitive if future resources expose generated values, so do not treat the state bucket as public operational documentation.

The baseline state bucket is private, uniform-access, public-access-prevention enforced and versioned. Legacy per-bucket access logging is not enabled by default; use the target organisation's Cloud Audit Logging requirements where procurement/security policy requires stronger storage-access evidence.

Business definitions and raw runtime credentials must not be intentionally stored in Terraform state.

## Recovery and audit

- Preserve the known-good Model Garden release, workspace revision and previous deployable image needed for rollback.
- Document ownership and backup/restore requirements for persistent Hermes/runtime data.
- Export relevant application, governed-action and cloud audit records to the client's or Model Garden's existing logging/SIEM stack when required.
- Use protected production GitHub Environments and business/operator approval before promotion.
- Test prohibited actions, approval-required actions, credential revocation, audit-chain verification and rollback as part of production acceptance.

## Assurance evidence

Every validated repository revision generates a commit-scoped Enterprise Evidence Pack. It includes canonical control status, automated scan summaries, evidence ownership/freshness metadata, Singapore assurance references and unresolved gaps. The exact scanner outputs are retained as a CI artifact.

The evidence pack does not convert architecture into a legal/compliance claim. Independent penetration testing, formal certifications, cyber insurance, an operating privacy/DPO programme, provider retention/training/residency terms and client-specific controls require their own current evidence.

## Threat boundary

The client workspace describes desired business behaviour but cannot grant itself new credentials or permissions. The governed Tool layer is the authority boundary for consequential external actions; cloud deployment identity is a separate infrastructure boundary; provider/model access alone never implies permission to act on business systems.

Do not add Kubernetes, a custom model gateway, a dedicated policy engine or a new tenancy/control-plane layer as a security reflex. Add infrastructure only when a concrete threat, workload or compliance requirement cannot be satisfied by the current smaller design.
