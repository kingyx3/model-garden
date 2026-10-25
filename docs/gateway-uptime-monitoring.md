# Optional client gateway uptime alert (#231)

The Terraform in `infra/docker-host/gcp/gateway-uptime.tf` is disabled by default. It reuses the existing HTTPS load balancer and Cloud Monitoring. It does not grant new IAM roles, expose a private VM, or create notification channels.

An authorized client operator must verify HTTPS DNS, TLS, `/healthz` (HTTP/load-balancer liveness), and `/health/gateway` (periodic-loop readiness), and supply an **existing approved channel in the same GCP project**. Only then set `gateway_ingress_enabled=true`, `gateway_domain`, `gateway_uptime_alert_enabled=true`, and `gateway_uptime_notification_channels`. Never apply to a live client without authorization.

A 60-second TLS-validated Cloud Monitoring uptime check now targets `/health/gateway`, not `/healthz`. The endpoint returns only `200 ok` when the gateway's own periodic loop has written a recent (at most 120-second-old) heartbeat without three consecutive failures of a fixed job, or `503 unavailable` for missing, malformed, future, stale or persistently degraded state. Responses are payload-free and `Cache-Control: no-store`; they never expose tenant names, job names, credentials or customer data. The load balancer and Docker still use `/healthz` for **HTTP liveness**, so a failed periodic job does not trigger automatic container restart. During a rolling upgrade the monitor may briefly report unavailable until the loop writes its first beat; plan and observe that transition. Per-location 60-second probe pass fractions are averaged across locations; an alert opens when the aggregate pass fraction stays **below 50% for 180 seconds**, or when missing data is treated as an active incident. This is **persistent majority probe degradation**, not proof that every gateway request failed. Isolated intermittent probe failures need not trigger. Incident-open and incident-close notifications are requested; Cloud Monitoring owns notification delivery and deduplication. An incident may auto-close after 30 minutes without proof of service recovery; verify healthy live probes and delivery before claiming recovery. Operators should inspect Cloud Monitoring, DNS/TLS and the gateway, then use approved private-host access to inspect workers and escalate to the client owner. Do not paste customer payloads or secrets into incidents.

**Coverage and evidence:** The gateway probe now detects a stalled periodic loop even when HTTP responds; the separately opted-in worker readiness probe is described below. Neither probe monitors the optional voice path. Mock-provider plans assert disabled-by-default behavior, rejection of cross-project/malformed channels, TLS, the readiness (not liveness) probe target, aggregate threshold, missing-data handling and opening/closing notification configuration. Synthetic HTTP tests cover healthy, missing, stale, future, malformed and persistently failed gateway heartbeats plus recovery and payload privacy. Mock plans cannot simulate Cloud Monitoring incident timing or prove recipient ownership and delivery. Neither Terraform apply nor a live notification drill has been proven here. Capture authorized failure/recovery notifications and delivery times before claiming operational readiness. Keep #231 open for voice coverage, worker/gateway failure-and-recovery drills, and real notification evidence.

## Opt-in governed-worker readiness coverage

The separate `worker_uptime_alert_enabled` flag (default `false`) adds a **second** Cloud Monitoring HTTPS check and alert for the governed worker. It is valid only when the gateway's existing client-scoped HTTPS ingress, uptime monitoring, and pre-authorized same-project notification channels are already enabled. Reuse those channels; do not add IAM grants, an exposed VM port, or a monitoring SaaS. **Before enabling**, verify the worker is deployed, its `worker/heartbeat.json` is on the same trusted governance volume as the gateway, and the client has approved this exact monitor and recipient. A client without a worker must leave the flag off.

`GET /health/worker` on the existing gateway ingress returns only `200 ok` when the worker heartbeat is recent (within 300 seconds) and has no persistent failed jobs. Missing, malformed, future-dated, stale or persistently degraded worker state returns `503 unavailable`; neither response includes tenant identifiers, job names, secrets or customer payloads. The existing `/healthz` remains gateway liveness and must **not** be used as worker proof. A shared-volume fault or a down gateway can also fail this check: use approved private-host diagnostics to distinguish causes. Do not use the worker probe as a container liveness restart trigger.

Cloud Monitoring probes the worker route every 60 seconds with TLS validation. A majority probe failure sustained for 180 seconds (or missing probe telemetry) opens an ERROR incident tagged client/environment/component; Cloud Monitoring sends `OPENED` notifications to the same approved channels as the gateway monitor. This deliberately does **not** send `CLOSED` as a recovery claim, because the provider can auto-close an incident without verified service recovery. Verify a newer healthy heartbeat, probe samples and real delivery before documenting recovery. An authorized operator must disable the flag when retiring the worker or client.

The mock Terraform plan checks opt-in isolation, channel reuse, TLS/path, threshold and missing-data behavior. Credential-free gateway regressions exercise healthy, missing, stale, future and persistent-failure responses. These are **offline code/IaC checks only**: no Terraform apply, provider notification, real failure/recovery drill, optional voice monitoring or per-tenant operating proof has occurred. Issue #231 remains open.

## Heartbeat filesystem integrity (v0.8.124)

The shared gateway/worker heartbeat writer now creates a random exclusive mode-0600 temporary file under a pinned, no-follow directory descriptor, fsyncs its content, and atomically replaces the heartbeat. This prevents the former predictable `.tmp` hardlink from truncating an unrelated file. Readiness checks open the directory and heartbeat without following links, reject hardlinks, FIFOs, nonregular and group/world-writable files, bound reads to 4096 bytes, and reject inode/directory replacement or in-place modification detected during a read. Missing or unsafe heartbeat data returns unavailable rather than a false healthy claim. These checks protect the local filesystem signal; they do **not** authenticate Cloud Monitoring probes, establish LiveKit voice readiness, prove live alert delivery, or prevent an attacker with the same filesystem owner privileges from rewriting the heartbeat legitimately. The volume and host access controls remain part of the security boundary. Adversarial regression tests are credential-free; #231 remains open for authorized failure/recovery and optional voice drills.

## Optional LiveKit worker native readiness (v0.8.125)

The reference voice overlay now uses the **pinned LiveKit Agents 1.8.8**
`AgentServer` built-in HTTP health endpoint on **container loopback only**,
`127.0.0.1:8081/`. The Python/Docker health check fails on HTTP 503,
connection refusal or timeout; it never exposes a host port, sends a bearer
token or publishes caller data. LiveKit's upstream health response checks its
worker connection and inference process, not whether any calls are in progress.
An idle but connected receptionist can be healthy. A process merely shown as
`running` is **not** sufficient: the voice overlay deployment waits for the
native Docker `healthy` state before declaring success, and restores the base
runtime if the bounded readiness wait fails. Docker Compose JSON arrays and
newline-delimited output are both accepted; malformed output fails closed.

For an **authorized** deployed reference host, inspect the private Compose
service's `voice` health state and the LiveKit control-plane worker
registration before enabling operational alerts. The check covers the local
LiveKit agent connection/inference readiness, **not** SIP trunk reachability,
audio quality, caller disclosure, human transfer, end-to-end call success or
notification delivery. No new GCP alert or public readiness endpoint was
enabled; opt-in Cloud Monitoring voice alert routing still needs a
provider-native/private signal and a client-approved live failure/recovery
drill. Do not mistake a successful offline test or a Docker healthy state for
real inbound-call proof. Keep #231 and #107 open until their live criteria
are satisfied.

## Voice overlay whole-stack readiness (v0.8.130)

The optional voice deployment now derives its required service list from the
**actual base and voice Compose definitions**, rather than trusting whatever
`docker compose ps` happens to report. A missing configured governed Tool
gateway, worker, channel gateway, coworker API or other service cannot be
mistaken for a successful voice deployment. Each declared healthchecked service
must be running and `healthy`; a service without a healthcheck must be
running. An absent optional service is not required. Duplicate/malformed
service records, a reported unhealthy or exited container, and a native LiveKit
readiness failure fail closed; startup transitions may wait within the bounded
deployment timeout. On failure the existing base-runtime restoration is
attempted. Credential-free tests cover missing, unhealthy, duplicated and
optional services. This is **deployment-time local readiness only**, not
continuous Cloud Monitoring voice alerting, real SIP call success or proof of
recovery; #231 and #107 remain open for approved live evidence.
