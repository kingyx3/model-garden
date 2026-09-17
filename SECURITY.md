# Security policy

Model Garden treats security defects in deployment, governance, authorization, isolation, secrets handling and auditability as product defects.

## Supported versions

Security fixes are made against the latest published Model Garden release and `main`. Client workspaces should pin a published release and upgrade through DEV/evaluation before PROD promotion. Older immutable releases remain available for rollback and evidence reconstruction, but are not a promise of indefinite security maintenance.

## Reporting a vulnerability

Do **not** open a public GitHub issue for a suspected vulnerability, leaked credential, exploit path or sensitive customer information.

Use GitHub's private vulnerability reporting / Security Advisory flow for this repository when it is available to you. Include:

- affected Model Garden release or commit;
- affected component and deployment shape;
- reproduction steps or a minimal proof of concept;
- expected versus observed security boundary;
- impact and any known exploitation;
- suggested mitigation, if known.

If private vulnerability reporting is unavailable, contact the repository owner privately through the account's published contact channel before disclosing technical details publicly.

## Handling expectations

Model Garden will triage reports by exploitability and impact, preserve relevant evidence, contain exposed authority or credentials, prepare a regression test where practical, and release a pinned fix through the normal validation pipeline. Client- or contract-specific notification timelines remain governed by the applicable agreement and law rather than by this repository policy.

## Scope

High-priority reports include authentication/authorization bypass, cross-client access, approval bypass/replay, secret disclosure, unsafe bootstrap/deployment authority, audit-evidence corruption, remote code execution, dependency/supply-chain compromise and material privacy leakage.

Provider outages, unsupported product configurations and social-engineering-only reports without a Model Garden control failure are generally handled through normal support rather than the vulnerability process.

## Disclosure discipline

Do not include real client data, credentials, caller content or other unnecessary sensitive information in a report. Use synthetic examples wherever possible.
