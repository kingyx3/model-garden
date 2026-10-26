# Tier 0 business-rules import (operator-assisted, credential-free)

The existing Tier 0 text knowledge store supports bounded UTF-8 `.txt`/`.md` content with section citations and governed `kb.search` receipts. This command adds a **local, private-client-workspace import step**; it is not a public upload endpoint, an authenticated owner-approval system, or proof of a live deployment.

From a published Model Garden checkout, with an existing **private** client workspace:

```bash
python3 scripts/import-knowledge.py /private/acme-workspace /private/uploads/services.md \
  --name service-guide --owner business-owner --agent receptionist \
  --classification internal --dry-run

python3 scripts/import-knowledge.py /private/acme-workspace /private/uploads/services.md \
  --name service-guide --owner business-owner --agent receptionist \
  --classification internal
```

The importer checks the input type, UTF-8 encoding, size and parseable sections with the existing knowledge parser. It refuses symlinked/hardlinked inputs, symlinked destination directories/Agent declarations, missing Agents, unselected `kb.search`, conflicting names or changed content. A per-workspace import lock prevents two cooperating imports from colliding. Repeating an identical import is idempotent; changing a source requires a separately reviewed version update. Output reports a digest, section count and warnings, not business text.

It creates `knowledge/service-guide.md` and `knowledge/service-guide.yaml` and adds the source name to the selected Agent's `spec.knowledge` list. **The new KnowledgeSource is always `draft`.** It cannot ground customer answers until an independently authorized client owner reviews the source, audience, classification, effective dates and private workspace diff, then changes `spec.status` to `approved` through the existing client-controlled change/promotion process. The `--owner` string is attribution, not authentication or signoff. The importer never deploys or enables a Skill, grants a provider credential, or writes to another workspace.

After approval, run workspace validation/compile and the usual DEV review and tests; only then promote through protected deployment. Confidential sources remain unavailable to public callers. Do not use `restricted` with Tier 0 because it has no document-level access control. Treat source files as client data: private repository, authorized access, retention and deletion review. A live end-user uploader, real inbound mailbox, deployed reference proof and broader #77 acceptance remain open.
