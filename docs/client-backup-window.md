# Bounded client backup maintenance window (#230)

This is a design for an **operator-approved, finite** stopped-project window, not an unattended backup service or production recovery guarantee. The operator must independently enforce a deployment/restart freeze, stop all client containers, and authorize a tenant-specific age recipient and GCS create-only writer. Host-side advisory locks do not prevent another deployment process from restarting a service.

The runner must reject an expired deadline, incorrect client/environment binding, insecure local state, concurrent invocation and running Compose services; create a unique private snapshot directory; call the existing age-encrypted snapshot and immutable GCS uploader; and emit redacted failure/recovery transition events. It must never stop or restart services, read age private identities, grant cloud permissions, silently retry partial uploads or claim live RPO/RTO.

Until the executable runner, positive/negative tests and final-head CI have passed, this document is a proposed operational contract only. External client-approved quiescence, an alert delivery channel, key rotation and a real replacement-host recovery drill remain open.
