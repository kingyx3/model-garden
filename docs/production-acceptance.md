# Production acceptance evidence gate

`scripts/production-acceptance.py` is a **read-only, fail-closed preflight** for a
specific client, environment, GCP project and immutable Model Garden release.
It supplements, rather than replaces, `scripts/operations-readiness.py`,
`scripts/reference-proof.py`, Terraform validation and the live operator runbook.

## Record format

Keep the manifest and small, redacted artifacts in an access-controlled operator
evidence directory **outside this repository**. Never commit credentials, raw
calls, customer payloads, private keys or unredacted logs.

```json
{
  "schemaVersion": 1,
  "client": "acme",
  "environment": "prod",
  "gcpProject": "acme-prod",
  "release": "0.8.192",
  "evidence": [
    {
      "kind": "keyless-redeploy",
      "observedAt": "2026-10-28T00:00:00Z",
      "artifact": "keyless-redeploy-redacted.json",
      "sha256": "REPLACE_WITH_SHA256_OF_ARTIFACT",
      "source": "live",
      "result": "passed"
    }
  ]
}
```

Include exactly one record for **every** kind enumerated by
`scripts/production-acceptance.py`: keyless redeploy, credential revocation,
rollback, real model turn, governed business action, Calendar transaction,
inbound voice, human fallback, monitoring alert, emergency pause, encrypted
backup, independent restore, audit export, client isolation and cost attribution.
The example above is intentionally incomplete and will fail the gate.

```sh
python3 scripts/production-acceptance.py /private/evidence/manifest.json \
  --client acme --environment prod --gcp-project acme-prod --release 0.8.192
```

The tool requires an explicit pinned MAJOR.MINOR.PATCH release and rejects mismatched identity, missing or duplicate categories, stale
(older than 30 days) or future timestamps, malformed/linked/oversized artifacts,
unsafe filenames, reused artifacts, changed SHA-256 content, mock evidence and failed results.
It prints machine-readable JSON without dumping artifact contents.

**Exit codes:** `2` means blocked; `3` means structurally complete but
`review_required`. There is **no automatic production-approved exit code**.
A locally authored JSON record and its hash cannot attest that a provider
transaction happened, that the evidence belongs to the client, or that the
operator was authorized. An independent authorized reviewer must verify
original provider/cloud receipts, identities, consent, tenant scope, recovery
outcomes, RPO/RTO, incident delivery and release/deployment provenance. Record
that sign-off in the client's controlled operating process, not in CI fixtures.

CI exercises adversarial tests of the preflight; CI does not fabricate live
acceptance or provision customer infrastructure. Use the canonical Confluence
roadmap and business launch checklist for final production promotion.
