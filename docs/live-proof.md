# Reference live proof and P0/P1 operating evidence

This is the executable evidence path for the remaining reference-deployment proof. Confluence owns the business readiness decision; this page owns exact repository mechanics.

## Encrypted, stopped-volume snapshot (issue #230)

For an **operator-authorized maintenance window only**, `scripts/client-volume-snapshot.py` inventories every Compose named volume (including separately named team/Agent volumes) and can archive those volumes without writing their plaintext payloads to host storage. It rejects writable host binds, unmanaged/external/anonymous volumes and mismatched Docker Compose ownership labels. The script checks no containers for that client project are running before and during capture; operators must also **freeze redeploy/restart automation** for the whole window. This check does not itself stop or restart services, and cannot prevent a second administrator racing to restart a container.

- Install **age** (maintained upstream) and Docker on the authorized host; provision a locally available, **digest-pinned**, tar-capable helper image. Use an offline-held age private key and approved public recipient. Do not hand Model Garden the private key or store it in the workspace.
- Inspect with `python3 scripts/client-volume-snapshot.py inventory --compose /path/to/bundle/compose.yaml --project acme-dev`. Confirm the client's deployed project and state inventory; the script does not infer the project from a customer-controlled file.
- In an authorized maintenance window, freeze deployment, stop **all** containers in the exact project (`docker compose -p acme-dev -f /path/to/bundle/compose.yaml stop`), then use a private host directory (mode 0700) as the output parent. Run `python3 scripts/client-volume-snapshot.py snapshot --compose /path/to/bundle/compose.yaml --project acme-dev --output /private/backups/acme-dev-unique-run --recipient age1YOUR_APPROVED_PUBLIC_KEY --image image.example/tar@sha256:PINNED_DIGEST`. Never re-use an output path.
- Only mark a snapshot complete if the tool exits 0 and writes `manifest.json`. Each `*.tar.age` archive is encrypted in a stream and listed with its encrypted SHA-256 hash and byte count; the manifest is private operational metadata, **not** a digital signature. Transfer complete encrypted files and the manifest to the authorized client-isolated backup bucket with the prescribed create-only identity. Independently verify the object hashes and record a separately authorized recovery test before assigning RPO/RTO. Failed captures retain partial encrypted files without a manifest; delete them only under the client's retention/incident policy.

**Not yet production-backup coverage:** This manual snapshot requires maintenance downtime; no automated schedule, upload, monitoring, independent chain of custody, operator-only restore procedure or real recovery drill is implemented. Snapshot encryption does not authorize decryption or restoration into a different client. [Issue #230](https://github.com/kingyx3/model-garden/issues/230) remains open until those paths are built and live-tested. Keep the client stopped only for the approved maintenance interval; explicitly return services to their approved operational state afterward.

## Create-only upload of completed encrypted snapshots (issue #230)

`scripts/client-backup-upload.py` uploads the completed **encrypted** snapshot from the stopped-volume maintenance procedure to the *existing, opt-in* GCP bucket in `infra/docker-host/gcp/runtime_backups.tf`. It does **not** take a new snapshot, schedule an automatic backup, grant cloud permissions, restore files, or prove recovery. The bucket must have been provisioned and approved for that client and environment; `gcloud` must already be authenticated through the client's approved, preferably keyless workload identity. The bucket-scoped `roles/storage.objectCreator` writer is enough for immutable creation: **no list, read, overwrite or delete access is required**.

Example, after a snapshot has completed successfully and its complete `manifest.json` exists:

```sh
python3 scripts/client-backup-upload.py \
  --compose /approved/bundle/compose.yaml \
  --project acme-dev \
  --source /private/backups/acme-dev-unique-run \
  --gcp-project-id approved-client-gcp-project \
  --client-slug acme \
  --environment dev
```

The tool validates client/environment binding and the exact Terraform-derived bucket name, rejects non-private or linked local files, checks the complete Compose volume inventory and encrypted SHA-256/byte sizes using the restore-side verifier, uploads each encrypted archive using `gcloud storage cp --if-generation-match=0`, rechecks local integrity and only then uploads the `manifest.json` **last** as a completion marker. It prints a minimal machine-readable success event; failures return nonzero with no cloud stderr or customer payload. A failed/partial run may have uploaded some immutable archives **without** a manifest and must be investigated rather than silently re-used; do not delete objects or retry the same object path automatically. Use a genuinely unique snapshot run directory for the next authorized attempt.

**Security and evidence:** Neither the script nor the snapshot manifest proves independent provenance of the sender, that an object is readable/restorable, or that an untrusted privileged host cannot alter files. Cloud Storage CLI transfer checks are not a substitute for an independently authorized object read-back and end-to-end recovery drill. The named cloud identity needs only create authority; operators validating recovery must use separate, explicitly authorized read credentials. Avoid granting broad `storage.objectAdmin` to the client runtime. Do not log encrypted object contents, private keys, personal data or secret-bearing external CLI output. Scheduling, failure notifications, cloud-side integrity read-back, customer-approved live recovery and measured RPO/RTO remain open in [#230](https://github.com/kingyx3/model-garden/issues/230).

## Independent cloud ciphertext read-back (issue #230)

`scripts/client-backup-readback.py` performs a **manually authorized, independent read** of exactly one client/environment/run backup under the Terraform-derived GCS bucket. Use a **different** client-scoped operator identity with `storage.objects.get` only (e.g. an approved bucket-level `roles/storage.objectViewer` binding; that broad role also grants listing, so restrict it with a custom get-only role where practical). Do not give this authority to the create-only runtime backup writer. This CLI does not modify or delete cloud objects, and it does not need Docker or an age key for ciphertext verification.

Example on an authorized operator host with existing `gcloud` read-only credentials, a private destination parent (mode 0700), and the **exact approved client Compose inventory**:

```sh
python3 scripts/client-backup-readback.py \\
  --compose /approved/bundle/compose.yaml --project acme-dev \\
  --gcp-project-id approved-client-gcp-project --client-slug acme \\
  --environment dev --run-id acme-dev-run12345 \\
  --destination /private/recovery/acme-dev-retrieved
```

The tool derives the only permitted GCS bucket and `v1/<project>/<run-id>/` prefix; it refuses a different project, cross-tenant run prefix, existing destination or unsafe storage parent. It looks up the **manifest** object and rejects missing/oversized manifests before download. It validates the manifest's exact Compose volume inventory and expected archive object names, queries object sizes, then retrieves each encrypted archive and independently checks SHA-256 and byte size. Only after every archive passes does it promote the downloaded manifest to `manifest.json` as the local completeness marker. On failure, the incomplete private destination must not be used; do not retry into the same path.

With an approved **offline age private identity** available separately, additionally pass `--identity /private/age-identity` to require authenticated archive decryption and safe TAR validation via the existing restore verifier; no plaintext TAR is written. Even with this option, the result **does not prove** snapshot source authenticity (the manifest is unsigned), that a recovery was tested on a clean host, any RPO/RTO or an active monitoring SLA. Cloud access, provenance and host credential custody remain governed operator activities. Keep [#230](https://github.com/kingyx3/model-garden/issues/230) open for automated scheduling, failure/recovery alerts, key lifecycle and an approved live recovery drill.

## Fail-closed operator restore of an encrypted snapshot (issue #230)

`scripts/client-volume-restore.py` is a separate **operator-only host CLI**, not a customer endpoint. It uses the existing stopped Compose-volume snapshot manifest and requires the age **private** identity to be held by an authorized host operator (mode 0600; never checked into the client workspace or given to the coworker). Run it on an **isolated, approved replacement or clean destination host**, not on a live client's existing data.

1. Provision the *same exact* client project name and a version-compatible Compose bundle on the intended destination. Create all named volumes with the Compose CLI (for example `docker compose -p acme-dev -f /approved/bundle/compose.yaml create`) **without starting services**, verify project ownership labels, and freeze all deployment/restart paths for the recovery interval. Every destination named volume must already exist and be **empty**. Do not repurpose another client's volumes.
2. Download the encrypted `*.tar.age` objects and `manifest.json` only from the client's authorized bucket to a protected operator directory. Check the bucket path and independently approved source snapshot/recipient fingerprint; manifest hashes are an integrity check, **not proof that a malicious operator could not replace both files**. Keep the age private key in a separate protected file. The restore verifier now requires the recovery source directory to be owned by the invoking UID and mode 0700, and the manifest, ciphertext objects and age identity to be UID-owned regular files with mode 0600 (or stricter), exactly one hard link and no symlinks. Prepare downloaded files under a private umask; do not relax ownership or modes to make a restore pass. This host-file trust check does not authenticate the cloud object's original publisher or replace an independent source-provenance receipt.
3. Run `python3 scripts/client-volume-restore.py verify --compose /approved/bundle/compose.yaml --project acme-dev --source /private/backups/approved-run --identity /private/age-identity`. This command does **not** touch Docker. It checks exact project and volume inventory, every encrypted SHA-256/byte count, age authenticated decryption, file/path-safe TAR contents, and rejects symlink/hardlink/device entries. No plaintext TAR is written to disk.
4. With all project containers stopped, use `python3 scripts/client-volume-restore.py restore --compose /approved/bundle/compose.yaml --project acme-dev --source /private/backups/approved-run --identity /private/age-identity --image image.example/tar@sha256:PINNED_DIGEST --confirm-project acme-dev`. The helper image must support `sh` and GNU-compatible `tar`. The script checks Docker Compose project/volume labels and emptiness before **any** writes, rechecks stopped state during application, then streams decrypted TAR through a pipe directly to the isolated helper. It refuses to overwrite populated volumes and **never starts or deletes containers or volumes**.
5. On failure, **do not start the client**. Some new volumes may be only partially restored; preserve evidence, reprovision clean empty volumes under an approved recovery/rollback procedure and retry with a verified, unchanged encrypted snapshot. On success, examine app/database consistency, ownership/permissions, ledger continuity, secrets and connector revocation/re-enrollment needs, and run the real business acceptance tests before an explicit operator-controlled start. Record observed RPO/RTO and authorization.

**Limitations:** Plain host Docker daemon authority is the operator access boundary; this CLI does not supply a separate identity provider or audit service. A malicious administrator can replace an unsigned manifest. Snapshot TAR member validation rejects links/devices, so volume data using such entries requires a separately reviewed recovery method. The resulting file ownership may need review before start. No automatic upload, scheduling, notification, verified GCP restore drill or backup SLA exists yet; keep [#230](https://github.com/kingyx3/model-garden/issues/230) open.

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
  --require-two-way-transcript \
  --require-fallback \
  --require-governed-action
```

The collector writes `.model-garden-live-proof/manifest.json` and `reference-proof.md`. It records service status and evidence counts/types only; it deliberately omits credentials, prompts, Tool arguments, caller identifiers and captured message bodies. With the strict flags, a pass requires a matched start/end receipt for the **same room and Agent**, a fallback receipt within a completed call's time window, no transcript-write failure, and an actual successful governed execution receipt (not just an authorization or denied action). When both voice and governed-action flags are set, the successful execution must also share an explicit `call:<roomHash>` correlation, Agent, initiating identity, Tool and request ID with an allowed (or human-approved) authorization for a **completed** call. Missing/ambiguous/reused IDs, unrelated background Tools and untagged time-window guesses do not satisfy this stricter gate. Duplicate starts/ends and unrelated fallback records do not satisfy these gates.

**Fail-closed evidence reads (v0.8.189).** The collector refuses unreadable/missing Docker evidence files, malformed or incomplete JSONL records, and ambiguous duplicate Compose service entries instead of silently dropping them. Strict acceptance also requires the actual voice/governed-Tool service when its corresponding gate is requested. Conflicting approval or execution outcomes for one governed request cannot be counted as a successful call action (a durable `started` followed by `succeeded` remains eligible). Operator errors include only generic evidence type/line information, never source log text. This is a *local structural completeness* check: the collector does **not** independently verify the Action Ledger hash chain, authenticate SIP or provider receipts, or prove the host was uncompromised; retain separately verified evidence for any live acceptance claim.

**Opt-in two-way transcript evidence (v0.8.209).** With `--require-two-way-transcript`, the collector reads the voice container's private default `/opt/data/transcripts.jsonl` and requires a single transcript paired to a completed call by room digest, Agent and ordered lifecycle timestamps. It checks nonempty ordered caller **and** agent turns, a recorded spoken disclosure, nonempty labelled summary, no dropped turns and no duplicate/conflicting transcript for the call. The report contains only aggregate counts; it does **not** export the speech or identifiers. A missing/unreadable transcript file fails the collector. If the deployment uses a non-default transcript path or an independently signed no-disclosure lawful-basis override, this structural gate cannot certify that variant: review it separately rather than passing a caller-controlled path or accepting an asserted override owner. Model-written post-call CRM summaries, interrupted TTS accuracy, human speech after cold transfer, provider receipts and real SIP identity still require independent verification. This is a code/test prerequisite for #107 and #117, **not** live proof.

**Same-call Scenario A correlation (v0.8.210).** When all three strict evidence flags `--require-two-way-transcript`, `--require-fallback` and `--require-governed-action` are selected, the collector now requires **one and the same completed call** to own the disclosed two-sided transcript, a successful explicitly call-correlated authorized Tool request, and an in-window human-transfer/message fallback. Separate positive events from unrelated calls can no longer combine into a false PASS. Reused room hashes with multiple Agents are ambiguous and do not count. Only an aggregate `referenceScenario.completeCalls` count is exported; no caller text, room hash or request ID. This is structural reconciliation, not proof of model-produced summary, provider-side completion, human receipt or live SIP audio.

**Collector PASS is a structural check, not live-provider attestation.** The collector cannot independently prove a SIP trunk connected, that both parties' speech was captured, that a provider performed an action, that a transcript was correctly redacted/filed, or that the client approved the outcome. Independently inspect the real call/transcript, upstream action and delivery receipts, tenant-scoped permissions, ledger chain, and client signoff; retain redacted source evidence with the engagement. A synthetic JSONL record can pass structural checks and must never be called a real live proof.

Keep the separate keyless-cloud evidence from a successful deployment **after** the temporary bootstrap credential has been revoked/deleted; the proof collector does not infer cloud identity from a running container.

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

## Reference call evidence attribution (issue #117, v0.8.223)

The LiveKit worker now stamps its configured Agent identity on successful
`HumanTransfer` and saved `FallbackMessage` lifecycle receipts. The reference
proof collector credits a fallback only when **room hash, Agent identity and
event time** match a completed call. A legacy receipt without an Agent identity
is still retained for operator investigation but **cannot satisfy** the
reference acceptance gate. Re-run the authorized DEV scenario on the upgraded
voice worker; do not backfill the missing identity by editing old evidence.

For two-way transcripts, every turn's monotonic offset must fit the recorded
start/end wall-clock duration (with at most one second for integer timestamp
rounding). A transcript whose speech extends beyond its own end timestamp
cannot establish completeness, even if its room hash and disclosure match.
These are **structural** checks on locally collected evidence, not SIP/provider
attestation, verified human delivery, owner consent or a client go-live signoff.
Retain independent provider receipts and real operator acceptance for #117.
