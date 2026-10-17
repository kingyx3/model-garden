# On-demand upstream connector discovery

`contracts/v1/integrations.json` is the only evidence-backed integration status authority. A public MCP listing is **not** an installed, approved, authorized, tested or tenant-live Tool. No vendor-specific SaaS connector is created by discovery.

Use the small operator CLI to query the [official MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers) directly, without a crawler, background synchronization or another status table:

```sh
python3 scripts/connector-candidates.py --root /private/operator/candidates refresh --search hubspot
python3 scripts/connector-candidates.py --root /private/operator/candidates list
```

Discovery fetches at most four pages of 50 records, from the pinned official HTTPS origin without redirects, under an eight-second timeout per page and a 1 MiB response bound. Failed or rate-limited lookups preserve the last good candidate book and report staleness. Results may be partial; the `truncated` field says so. The registry is an **untrusted** catalog even when its publisher field looks official.

For other selected vendor-maintained catalogs, obtain the source's current `servers` export through the vendor's own published route, check its publisher/provenance out of band, and import a local JSON object whose `servers` array contains server metadata. **No arbitrary vendor feed URL is fetched by the sidecar**, and importing grants no runtime access.

```sh
python3 scripts/connector-candidates.py --root /private/operator/candidates \
  import-json /private/approved/vendor-servers.json --source vendor-name
```

The local book records provenance, publisher, version/digest, source dates, reviewed findings and reviewer decisions. Missing license, a non-pinned version, malicious metadata, excessive scopes, unsafe endpoints and schema/version drift block or reset review. Upstream MCP registries may not supply license, full Tool schemas or scope proof; supply and independently verify the missing evidence before review. A passing contract test only permits `contract-tested`, not deployment.

```sh
python3 scripts/connector-candidates.py --root /private/operator/candidates review <candidate-id> --by reviewer@example.com
python3 scripts/connector-candidates.py --root /private/operator/candidates contract-tested <candidate-id> \
  --by reviewer@example.com --digest <sha256-of-test-evidence>
python3 scripts/connector-candidates.py --root /private/operator/candidates deny <upstream-name> \
  --reason unsafe --by reviewer@example.com
```

Before a candidate can reach a coworker, separately pin an exact operation/version/schema in the canonical integration registry, map only defined portable Tools, assess license/supply chain, hosting/data path, maintenance, least-privilege auth and rollback, and require reviewed tenant consent and an **individually isolated coworker grant** (#129). The governed execution gateway (#158/#87) must enforce action approval and audit. Client-defined private MCP/API connections (#159) remain tenant-scoped and never appear in this public candidate list. A successful public registry request does not test or establish any live provider connectivity.
