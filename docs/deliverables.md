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
