# Enterprise assurance and Singapore DDQ answers

Model Garden keeps a version-controlled assurance layer for common enterprise security, privacy and AI-governance due-diligence questions. Its purpose is to answer quickly **without turning architecture intent into an unsupported compliance claim**.

## Files

- `assurance/evidence.yaml` — canonical Model Garden control statements, current evidence status, repository evidence and limitations.
- `assurance/ddq-catalog.yaml` — common enterprise DDQ questions and the controls needed to answer them.
- `assurance/singapore.yaml` — Singapore regulatory/guidance map and procurement evidence targets.
- `scripts/ddq.py` — single-question, CSV batch and readiness-gap interface.

Confluence remains the business/security policy authority. These repository files are the executable/evidence-facing representation used to prepare DDQ answers and identify proof gaps.

## Answer one question

```bash
python3 scripts/ddq.py answer "Where is our data stored and can it stay in Singapore?"
```

The result contains:

- matched canonical DDQ ID;
- assurance state (`supported`, `review_required` or `manual_review`);
- evidence-backed answer text;
- repository evidence paths;
- applicable Singapore law/guidance/certification references;
- an explicit human-review warning when the answer depends on deployment, legal entity, sector, provider contract or independent assurance.

Use JSON when another tool needs to consume the answer:

```bash
python3 scripts/ddq.py answer --json "Do you use customer prompts to train models?"
```

## Answer a customer questionnaire CSV

Input must have a `question` column; all other customer columns are preserved.

```bash
python3 scripts/ddq.py answer-file customer-ddq.csv /tmp/customer-ddq-answered.csv
```

Never send the generated file without reviewing every row marked `review_required=true`. The generator is designed to be conservative: if it cannot support a claim from the evidence registry, it says so.

## Show enterprise-readiness gaps

```bash
python3 scripts/ddq.py readiness
```

This is the engineering/evidence backlog, not a certification score. Typical gaps that should remain visible until real evidence exists include independent penetration testing, formal certifications, company-level cyber insurance, exact provider data-residency statements, incident-response commitments and live deployment proof.

## Singapore source hierarchy

Use official sources and treat each according to its actual legal status:

1. **PDPA / PDPC** — core personal-data obligations and regulator guidance where applicable.
2. **PDPC AI personal-data guidance** — AI development/deployment when personal data is involved.
3. **IMDA Model AI Governance Framework for Agentic AI** — primary Singapore governance reference for agentic AI; guidance, not a general AI statute.
4. **IMDA / AI Verify Foundation GenAI governance and AI Verify** — broader generative-AI assurance references.
5. **CSA Cyber Essentials / Cyber Trust (SS 712:2025)** — voluntary independent cybersecurity certification targets; never claim certification without a current certificate.
6. **PDPC Data Protection Trustmark (SS 714:2025)** — voluntary privacy assurance target; never claim certification without a current certificate.
7. **Cybersecurity Act** — conditional legal overlay for CII and other regulated categories; assess actual applicability.
8. **MAS technology-risk requirements** — conditional financial-sector overlay; determine the client's exact regulated entity and applicable notice/guidance.
9. **Health Information Act / healthcare cyber and data-security requirements** — conditional healthcare overlay.

The authoritative URLs and current scope notes are maintained in `assurance/singapore.yaml`.

## Evidence discipline

An answer can be strong without being a blanket `yes`. Use these patterns:

- **Implemented:** explain the control and point to code/tests/configuration.
- **Implemented; live proof pending:** say exactly which final operational evidence has not yet been retained.
- **Client-configured:** describe the supported control and identify the facts that must be checked for the client's providers/environment.
- **Policy required:** describe the intended operating requirement and do not imply continuous evidence that does not yet exist.
- **Not verified:** say that no current evidence is on file. Do not infer certification, insurance, pen-test status, residency or provider-training terms.

## What this capability should make easy

A typical enterprise DDQ should be answerable from the same small evidence set: architecture/isolation, IAM, secrets, encryption, SDLC, vulnerability management, logging, incident response, business continuity, data flows/retention/residency, subprocessors, offboarding, privacy role, AI provider data use, agent authority, human oversight, evaluation, prompt-injection controls and formal-assurance status.

Sector-specific requirements stay overlays. Do not burden every normal SMB deployment with MAS, HIA/CII or certification requirements until the client/use case actually triggers them.
