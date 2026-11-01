# Governed report, spreadsheet, deck and PDF deliverables (#101)

The reference `create-deliverable` Skill is reusable across Agents, but is
**disabled by default** for `account-researcher`. Its only allowed Tool is
`docs.create`; its required `docs` capability must be bound and ready before
activation. Enabling a Skill without a supported binding fails closed.

## Opt-in for a no-SaaS client

1. In a reviewed client workspace, bind `docs` to
   `built-in-artifacts` at tier 0 for the intended environment. Ensure
   the compiled client slug, retention policy and operator approval identity
   are correct. Do not edit the shared reference defaults to enable every
   client automatically.
2. Remove `create-deliverable` from the Agent's `disabledSkills` after
   owner review. The Agent must select both the Skill and the
   `docs.create` Tool. Compile and review readiness before deployment.
3. Request a DOCX/PDF report (`sections`), XLSX workbook (`sheets`),
   PPTX deck (`slides`) or Markdown document. The Skill asks the
   governed `docs_create` Tool to persist the file and records bounded
   source identifiers. Exact owner approval is required for writes.
4. Only after `executed`, reply with the returned
   `tier0:<client>/a-...` document ID. The verified channel gateway
   can attach a file belonging to the mapped requester; Slack upload
   requires explicit `uploadFiles: true` and the relevant client-scoped
   bot permissions. Other surfaces show a private reference, not a
   universally accessible URL. The operator may retrieve it using the
   private artifact-store tools.

## Offline evaluation

```sh
python3 -m unittest discover -s tests -p test_deliverable_skill_evals.py -v
```

`evals/deliverables/scenarios.json` contains two synthetic examples:
a source-attributed DOCX operating report and an XLSX activity workbook
with a formula-looking string. The tests enable the Skill only in a
temporary workspace, require a Tier 0 docs binding, exercise the
**actual governed approval/execution path**, verify the file package and
ledgered location/source references, and reject modified approval
payloads, public callers and cross-tenant reads. The normal
`contracts-and-runtime` CI test discovery runs them.

**Boundaries:** This is credential-free contract evidence, not an
LLM-generated report, a real client request, viewer-compatibility
signoff, a Google Docs or Microsoft 365 native-file conversion, or
Drive/SharePoint delivery. The current built-in `docs.create` runtime
does not dispatch to an upstream document provider. Provider-native
formats, client-approved connected destination, verified channel
delivery and a real client scenario remain open on #101/#91.
Reuse an approved upstream MCP/provider integration rather than
introducing bespoke SaaS connectors.

## Tier 0 artifact retention and retry provenance (v0.8.251)

The private artifact index is not a durable public catalog. `files.search` and the
operator's metadata listing exclude expired entries immediately, even before
`runtime-artifacts.py purge-expired` physically deletes the payload. A shorter
reviewed retention policy takes effect on previously created files; increasing
retention does not revive artifacts past their original expiry. The governed worker's `retention` job runs physical expiry cleanup on its
configured interval; operators must verify the job's health and ledger receipts
for actual erasure. The host-local `runtime-artifacts.py` operator CLI now uses
the same reviewed `MODEL_GARDEN_ARTIFACT_RETENTION_DAYS` (default 365) for
`list` and `purge-expired` instead of silently reverting to 365 days. An explicit
`--retention-days` can only tighten this policy; invalid configured retention
fails closed even when an override is supplied. This prevents an operator's
metadata view or manual purge from treating shortened client retention as a
longer default. Existing artifacts retain their original maximum expiry.

A repeated write returns an existing artifact only when **content digest, name,
requester, producing Agent, media type, classification and source references**
all match. A changed provenance field creates a separately audited artifact
instead of silently returning an older object with misleading metadata. This is
host-local credential-free behavior, not independently immutable evidence or
proof of real tenant delivery and retention enforcement.

## Artifact creation audit failure boundary (v0.8.259)

A new Tier 0 file is not published in the tenant index until the
`artifact.create` audit hook accepts its redacted creation receipt. If the
hook fails before accepting the receipt, the new payload is removed and the
index is not updated; an identical retry must create a new accounted artifact,
not return an unledgered duplicate. Existing artifacts remain readable.

This is a **host-local fail-closed prerequisite**, not a distributed transaction
with an independent immutable ledger. A crash or storage failure after the
receipt was accepted but before the index was committed can leave a receipt
without a published artifact; an audit sink that writes and then raises can
also leave an uncertain receipt. In either case, operators must reconcile the
private ledger and artifact inventory before retrying or asserting completeness.
It does not establish provider-live delivery, retention erasure in backups or
client acceptance (#77/#101).

## Deletion and expiry audit preflight (v0.8.267; #77)

Tier 0 manual deletion and the worker's scheduled retention sweep now emit
redacted `artifact.delete.prepare` / `artifact.expire.prepare` intents **under
the tenant lock before unlinking**; the existing `artifact.delete` /
`artifact.expire` events follow successful removal under that same lock.
The expiry sweep validates **every** candidate's private payload path before
emitting intents or removing any file. If an intent cannot reach the ledger,
the batch aborts without deleting payloads or changing the index. The operator
should reconcile each prepare with a matching completion, rather than counting
an intent as evidence of physical erasure.

This is still a host-local, non-transactional filesystem and ledger boundary.
A crash, a storage failure during unlink/index publication, or a ledger sink
that accepts an event and then raises may leave unmatched intents or a stale
index. Such a state requires manual inventory/ledger reconciliation before
retrying or claiming retention compliance. The sweep does **not** erase cloud
backups, attest independent ledger storage, prove a live retention drill or
replace the client owner's retention and erasure approvals.
