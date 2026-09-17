# Reference live proof and P0/P1 operating evidence

This is the executable evidence path for the remaining reference-deployment proof. Confluence owns the business readiness decision; this page owns exact repository mechanics.

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
  google_calendar:
    token_ref: secret://google/dev-calendar
    calendar_id: primary
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
export MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN='...'
export MODEL_GARDEN_GOOGLE_CALENDAR_ID='primary'  # optional
export LIVEKIT_URL='wss://...'
export LIVEKIT_API_KEY='...'
export LIVEKIT_API_SECRET='...'
export LIVEKIT_SIP_TRUNK_ID='...'
python3 scripts/preflight-live-mvp.py --json
```

The preflight checks presence only and never prints secret values.

## Evidence-producing acceptance

After the runtime is deployed, run the normal acceptance scenario:

1. make a real inbound SIP call;
2. complete at least one real model turn;
3. run Calendar availability and one approval-gated booking or other governed action;
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

It fails closed until runtime/cloud/provider ownership, break-glass and recovery contacts, support/escalation, retention/recording/vendor handling, operator continuity, rollback evidence and backup/restore requirements are explicit.

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
- real GCP/model/Calendar/LiveKit credentials and the post-revocation keyless redeploy;
- an actual inbound call and real business transaction;
- real client economics, ROI, testimonial/case-study/referral permission and pipeline outcomes.

These should stay visibly incomplete until evidence exists. The repository now makes the technical/operating proof repeatable and machine-checkable instead of relying on an informal checklist.
