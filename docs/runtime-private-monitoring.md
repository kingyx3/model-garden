# Private gateway and worker readiness samples (opt-in; #231)

`scripts/runtime-health-metric.py` is the **preferred private monitoring path**.
As of v0.8.198, HTTP `/health/gateway` and `/health/worker` require the
**actual loopback TCP peer** by default, including when the gateway is published
through a reverse proxy. `X-Forwarded-For`, `X-Real-IP` and `Host` do not
authorize these routes. A reverse proxy sharing the gateway's network
namespace could itself connect from loopback; such a proxy must separately
deny external `/health/gateway` and `/health/worker` requests. This guard is
not a substitute for network isolation. An approved host timer
finds exactly one Compose `gateway` container and executes fixed Python code
**inside that container**, fetching `127.0.0.1:8080/health/gateway` and
`/health/worker`. The existing gateway-loop and worker heartbeats decide
readiness. Missing, failed or duplicate gateway containers and stale/failed
loopback probes publish `0`. The only outbound data are **two boolean GCE
Cloud Monitoring metrics** with the VM's existing `metricWriter` identity.
No public ingress, new IAM role, external monitoring SaaS or client payload
is required. Docker output and metadata tokens never enter logs.

The host must run a trusted approved script and have local Docker access.
This is not SIP, provider action, client ownership or delivered alert proof;
a compromised Docker daemon/host can forge its own signal. A broken sampler
cannot publish `0`; the alert treats **missing telemetry** as an incident,
once an initial metric sample exists.

For an **authorized** client-isolated GCE DEV host, have the client owner and
named operator approve the GCP project, Compose name, metric identity,
notification channels and cadence. From a trusted checkout, first run:

```bash
python3 scripts/runtime-health-metric.py --project-id sample-project-123 \
  --client acme --environment dev --compose-project acme-dev
```

Verify **both** new metric series and exact instance/client/environment labels
in Cloud Monitoring. Install a root-owned systemd oneshot unit with that exact
command, after Docker/network readiness, and a timer with `OnBootSec=60s`,
`OnUnitInactiveSec=60s`, `AccuracySec=10s`. Secure unit and script
permissions, then explicitly enable the timer. Only after actual first samples
and approval, set `runtime_private_alert_components=["gateway","worker"]`
(or the subset actually deployed) and the existing same-project
`gateway_uptime_notification_channels`. Review and apply
`infra/docker-host/gcp/runtime-private-monitor.tf` through the normal
client-authorized Terraform process. The disabled-by-default alert opens after
three minutes of unhealthy/missing samples and sends `OPENED` only, not a
false auto-close recovery claim. Retire the alert via approved Terraform
change before removing its timer or worker.

The live acceptance drill must separately stop/stall gateway and worker,
capture sample, provider incident, **actual recipient delivery and operator
acknowledgement**, restore both, and verify newer healthy samples. Record
escalation owner, measured latency and retirement. Offline tests and accepted
metric API writes are not notification delivery or tenant-live proof.
Existing legacy public uptime checks are **not** compatible with the secure
default. Before upgrading a client that opted into those checks, obtain owner
approval and migrate to the private metric publisher and approved channels;
verify new samples and notifications before disabling the old alerts. A
transitional legacy public probe requires both the reviewed binding setting
`spec.gateway.readiness_exposure: legacy-public` and Terraform
`gateway_legacy_public_readiness_acknowledged=true`, with the existing
`gateway_uptime_alert_enabled=true` and approved channels. This compatibility
mode intentionally exposes only payload-free readiness status but does **not**
satisfy the original private-endpoint security criterion. Record an explicit
retirement date; never assume this is a production-approved configuration.
No timer, Terraform plan or cloud policy is applied by this change.
