# Reference live proof and P0/P1 operating evidence

This is the executable evidence path for the remaining reference-deployment proof. Confluence owns the business readiness decision; this page owns exact repository mechanics.

## Worker liveness versus operational readiness (issue #231)

The optional governed worker writes `/opt/data/.modelgarden/worker/heartbeat.json` with a recent timestamp and **job names only** (no message text, contact details, provider errors or credentials). The Compose `--health` check continues to mean *the worker loop is alive*, not that every optional integration is working. New `--readiness` emits only `status`, `reason`, `failedJobs` and `persistentFailures`, returning non-zero for a missing/stale/invalid heartbeat or three consecutive failed **attempts** of a job. Skipped long-interval jobs retain their previous failure condition until the next successful attempt; recovery clears it. A timestamp from over 30 seconds in the future is rejected.

On an authorized client host, run `docker compose exec -T governed-worker python /opt/model-garden-platform/platform/runtime/worker.py --readiness` from the deployed bundle directory. Use the exit status for operator diagnostics; no customer content enters the output. If the worker was never enabled, no heartbeat exists and this probe must not be used as an alarm for that client. These code checks are **not** Cloud Monitoring uptime checks, alert delivery or live availability evidence. [Issue #231](https://github.com/kingyx3/model-garden/issues/231) remains open for authenticated per-client notification channels, voice/gateway checks, deduplication and real failure/recovery drills.

## What is now implemented

The managed GCP deployment can optionally run the Receptionist through a LiveKit voice worker while keeping Hermes as the reasoning/runtime authority. The voice worker:

- joins LiveKit dispatch as `model-garden-receptionist` by default;
- uses OpenAI STT/TTS for the reference path and Hermes' authenticated OpenAI-compatible API for reasoning;
- exposes deterministic human transfer through LiveKit `TransferSIPParticipant`;
- falls back to minimal message capture when transfer is unavailable or fails;
- records redacted call lifecycle / transfer / message-evidence records on its isolated Docker volume;
- is added as a Compose overlay only when the environment selects `channels.voice.provider: livekit`;
- restores the base Hermes + governed-Tools runtime if the voice overlay fails health verification.

This does **not** make a simulated call count as production proof. A real LiveKit/SIP call, real provider credentials and a real fallback path must still be exercised before the corresponding business-readiness item is marked proven.

## Voice environment binding

Add the provider-neutral business/runtime selection to the client environment. Credentials remain logical references:

```yaml
environment: dev
runtime:
  profile: receptionist
  model_credential_ref: secret://model/dev
connectors:
  kb: {provider: text-knowledge, tier: 0}
  crm: {provider: built-in-crm, tier: 0}
channels:
  voice:
    provider: livekit
    url: wss://your-project.livekit.cloud
    api_key_ref: secret://livekit/dev-api-key
    api_secret_ref: secret://livekit/dev-api-secret
    agent_name: model-garden-receptionist
    human_transfer_target: tel:+15551234567
```

The matching protected GitHub Environment secret map must contain exactly the referenced keys. The LiveKit URL and transfer destination are configuration, not credentials.

## External-resource preflight

The local/reference preflight now matches the actual runtime credential contract:

```bash
export OPENAI_API_KEY='...'
export LIVEKIT_URL='wss://...'
export LIVEKIT_API_KEY='...'
export LIVEKIT_API_SECRET='...'
export LIVEKIT_SIP_TRUNK_ID='...'
python3 scripts/preflight-live-mvp.py --json
```

The preflight never prints secret values. Until the upstream governed Calendar execution path and a real tenant proof are implemented, it explicitly reports booking as **blocked**, even if a legacy Calendar token is present.

## Evidence-producing acceptance

After the runtime is deployed, run the normal acceptance scenario:

1. make a real inbound SIP call;
2. complete at least one real model turn;
3. run an implemented governed action. Calendar availability/booking remains blocked until a vetted upstream MCP provider and separate tenant proof exist;
4. exercise either the human-transfer path or message-capture fallback;
5. verify the call ends cleanly;
6. run the proof collector on the Docker host.

```bash
python3 scripts/reference-proof.py \
  --project-name acme-dev \
  --require-voice-call \
  --require-fallback \
  --require-governed-action
```

The collector writes `.model-garden-live-proof/manifest.json` and `reference-proof.md`. It records service status and evidence counts/types only; it deliberately omits credentials, prompts, Tool arguments, caller identifiers and captured message bodies.

A successful proof pack is evidence for the live runtime/channel/governance path. Keep the separate keyless-cloud evidence from a successful deployment **after** the temporary bootstrap credential has been revoked/deleted; the proof collector does not infer cloud identity from a running container.

## Per-client P0 operating record

Copy `templates/operations/client-operations.yaml` into the private engagement/operations workspace and fill the real owners and policies before production. Validate it with:

```bash
python3 scripts/operations-readiness.py client-operations.yaml
```

It fails closed until the client identity, runtime/cloud/provider owners, break-glass and recovery contacts, credential revocation, support coverage, alert routing, retention/recording/vendor handling, operator continuity, rollback/backup requirements, offboarding owner, and deployment/agent/action/fallback acceptance evidence are recorded. All `REQUIRED`/`TODO`/`TBD` placeholders must be resolved. For a workflow without voice, explicitly document why voice-specific evidence is not applicable rather than inventing a call or leaving a placeholder. A populated record is still not a substitute for independent inspection of the linked evidence.

## P1 economics and repeatability

Start a private scorecard:

```bash
python3 scripts/operating-scorecard.py init model-garden-operating-scorecard.yaml
```

Update it from real engagements, then summarize:

```bash
python3 scripts/operating-scorecard.py summary model-garden-operating-scorecard.yaml
```

The scorecard calculates actual direct cost and gross margin, sales/discovery/implementation/tuning/support effort, time-to-working-DEV, time-to-production, cash collected/receivables, provider costs, reusable/configurable/bespoke mix and largest-client revenue concentration. Baseline/post-launch value and next Expansion evidence remain real-client inputs; the script does not fabricate them.

## What code cannot honestly close

The following remain external evidence or founder actions rather than missing platform code:

- legal entity, bank/payment collection, bookkeeping, tax/invoicing setup;
- legal review of commercial/privacy terms for the operating jurisdictions and client sectors;
- insurance purchase/decision;
- real GCP/model/LiveKit credentials, an approved upstream integration with per-coworker scoped authorizations for each chosen business system, and the post-revocation keyless redeploy;
- an actual inbound call and real business transaction;
- real client economics, ROI, testimonial/case-study/referral permission and pipeline outcomes.

These should stay visibly incomplete until evidence exists. The repository now makes the technical/operating proof repeatable and machine-checkable instead of relying on an informal checklist.
