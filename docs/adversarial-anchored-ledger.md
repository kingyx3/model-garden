# Independent exported-ledger correlation for adversarial evaluations (#235 / #80)

`verify-adversarial-ledger.py` is an **offline, read-only supplementary check** for a separately recorded `recorded-model-run` trace. It first verifies a snapshot of exported Action Ledger records and head manifests against a **head sequence and SHA-256 retained independently of the runtime/exporter** using the existing `ledger_export.verify_remote_export`. It then correlates each synthetic attack's claimed Tool and ledger outcomes to actual chained v3 events using the first-party `governance.correlationId`, client, environment and Agent. It refuses missing/extra/conflicting receipts, unauthorized executions, wrong-tenant/Agent records, reused request IDs, unresolved `execution/started` and unanchored denials. The existing adversarial scorer still checks forbidden Tools, canary leakage and every hostile model turn.

## Controlled run

Use an **authorized non-production** environment with synthetic canaries. Arrange for the governed Tool boundary to emit unique correlation IDs for each attack; do not use model-supplied identity or correlation metadata. An independent reviewer must pin the export head from a separately controlled channel and approve the case-to-correlation mapping. Example private manifest (all cases in the selected suite must be present):

```json
{
  "schemaVersion": 1,
  "tenantId": "reference-dev",
  "environment": "dev",
  "agent": "receptionist",
  "cases": [
    {"id": "forged-owner-multiturn", "correlationId": "adv-run-01"}
  ]
}
```

The example above is **incomplete** for the default seven-case suite and deliberately fails until all cases are bound to distinct IDs. After independently exporting the governed ledger to a read-only directory, retaining the trusted head witness and preparing the separately reviewed trace:

```bash
python3 scripts/verify-adversarial-ledger.py \
  --trace /private/evidence/recorded-model-run.json \
  --bindings /private/evidence/case-bindings.json \
  --sink-dir /private/ledger-export \
  --expected-head-sequence 123 \
  --expected-head-hash '<64-character independently pinned lowercase SHA-256>'
```

Use `--expected-first-sequence` only with an independently justified legacy v1 prefix. The verifier reads one snapshot of exported records and heads, checks that exact snapshot, and uses stable failure codes only; no model text, customer data, paths or provider errors are printed. It never writes to the export, ledger or provider. The `--sink-dir` must be a separately administered read-only export, not the runtime's mutable local ledger. Input traces and bindings must be private, with independent custody of the pinned head.

**Proof boundaries:** A successful result proves only consistency of the supplied trace's Tool receipts with the *exported* chain under the independently supplied witness and case bindings. It does **not** attest the model, prove that every hostile turn was sent, prove complete provider-side effects, establish provider identities, validate a human handoff or show live SIP/mail/retrieval safety. Uncorrelated actions with unrelated request IDs cannot be ruled out without independent channel/provider observation. A trace with no Tool calls may pass the join without proving any model execution. The independent witness is an operator input, not cryptographically signed by this CLI; verify its provenance separately. The existing export verifier does not establish local-ledger truncation proof from the remote chain alone. Real approved model execution, retrieved content, voice/mail adversarial runs, independent provider receipts and client acceptance remain required before closing #235.
