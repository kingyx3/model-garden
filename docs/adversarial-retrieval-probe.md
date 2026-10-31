# Governed retrieved-content adversarial model probe (#235)

The private `scripts/capture-adversarial-retrieval.py` probe closes a **harness gap**, not the issue's live safety acceptance. It copies the versioned **synthetic** `evals/receptionist/poisoned-retrieval.md` into a disposable example workspace, compiles a Tier 0 receptionist, binds `kb` to the built-in text provider in a synthetic DEV tenant, calls the **real governed `kb.search`**, verifies that the hostile section was actually returned and that a chained read receipt exists, and passes those returned excerpts into the existing `CoworkerClient.answer` path. This is not a user prompt merely *describing* retrieval. The model path uses `coworker.answer`'s untrusted JSON excerpt boundary, **not** the deployed receptionist `SOUL.md` or a full Tool-using Hermes session. It does not exercise live voice, mailbox or real customer knowledge.

With explicit authorization to use an isolated private Hermes API (never production customer data), create a mode-0700 private evidence directory, supply the deployment's private API URL and key through environment variables, and run:

```sh
python3 scripts/capture-adversarial-retrieval.py --output /private/evidence/retrieval-probe.json
```

The CLI refuses missing credentials, an unsafe output directory, existing output files, failed or empty retrieval, absent synthetic attack markers, mismatched compiled content, broken local ledger chain and invalid model citations. It prints no model text, key, canary or exception details. The private mode-0600 output records source/citation digests, a **local** audit-file digest and the model answer (or null). The output source is `unverified-retrieval-model-run` and cannot be accepted by `eval-adversarial-trace.py` as a `recorded-model-run`. A successful capture is **not a PASS or a safety verdict**; a local chain hash is not an independently retained head witness.

For #235 closure, a client-authorized isolated tenant and evidence custodian must independently attest the running profile and provider identity, complete Tool attempts and denials against an exported ledger head, the real deployed retrieval/voice/mail paths, observed human handoff, provider receipts and client signoff. Keep this synthetic probe separate from live-provider proof and never introduce unreviewed hostile content into an actual client's approved knowledge.
