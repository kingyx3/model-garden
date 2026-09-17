# Enterprise evidence pack

Model Garden generates a customer-reviewable enterprise assurance pack from the same version-controlled evidence used by the DDQ answerer. The generated pack is an evidence index, not a compliance certificate.

## Generate locally

```bash
python3 scripts/enterprise-evidence.py verify
python3 scripts/enterprise-evidence.py pack --output /tmp/model-garden-enterprise-evidence
```

Or:

```bash
make enterprise-evidence
```

The output includes:

- `enterprise-evidence-pack.md` — human-readable control/evidence summary and explicit review gaps;
- `evidence-manifest.json` — machine-readable release, control-state and automated-check results;
- `secret-scan.json` — high-confidence repository secret-pattern scan;
- `terraform-guardrails.json` — focused Terraform public-CIDR review evidence;
- `sbom.cdx.json` — CycloneDX inventory of repository-declared Python dependencies and Docker base images.

CI also runs `pip-audit` against the runtime Python requirements and passes its JSON result into the generated manifest. The CI artifact is therefore the preferred retained evidence for a release.

## Evidence rules

`assurance/evidence.yaml` remains the canonical control/evidence registry. `scripts/enterprise-evidence.py verify` fails when an implemented control points at missing repository evidence or uses an unknown evidence state.

The pack must keep unresolved assurance visible. In particular, repository automation does not establish:

- a current independent penetration test;
- ISO, SOC 2, CSA Cyber Essentials/Cyber Trust or DPTM certification;
- company cyber-insurance coverage;
- a provider's exact residency, retention or training terms;
- client-specific encryption, SSO, SIEM, retention or business-continuity configuration;
- legal compliance for a specific entity, deployment or sector.

Those facts stay `not_verified`, `client_configured`, `policy_required` or `implemented_live_proof_pending` until the required evidence exists.

## CI evidence

The `enterprise-assurance` validation job:

1. validates the evidence registry and referenced repository paths;
2. runs the repository secret and Terraform guardrail scans;
3. runs `pip-audit` against `requirements-runtime.txt`;
4. generates the evidence pack and SBOM;
5. uploads the result as a retained GitHub Actions artifact.

A dependency vulnerability causes the CI dependency-audit step to fail rather than silently producing a clean-looking pack. Terraform guardrails are deliberately narrow and do not replace Terraform validation, an external IaC scanner or independent security testing.

## Customer use

Before sending a generated pack externally, review deployment-, provider-, legal-entity- and sector-specific statements. Attach independent reports or certificates separately only when current and in scope. Do not hand-edit generated output into a second source of truth; update the evidence registry or implementation instead and regenerate it.
