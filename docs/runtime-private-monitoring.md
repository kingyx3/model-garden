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

The loopback sampler requires an **exact HTTP 200** from each component even when the gateway container runs Python with optimization (`-O`/`PYTHONOPTIMIZE`); any other status or probe failure publishes unhealthy rather than silently accepting a non-200 response. This is a local metric integrity check, not proof of alert delivery.

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


## Review-only host timer bundle (v0.8.214 prerequisite)

Instead of manually composing a host timer, a named operator can render a
**review-only** tenant-bound systemd bundle from a trusted release checkout:

```bash
python3 scripts/render-runtime-health-units.py \\
  --client acme --environment dev \\
  --compose-project acme-dev --confirm-compose-project acme-dev \\
  --project-id sample-project-123 --confirm-project-id sample-project-123 \\
  --service-user mg-monitor
```

The output is JSON containing a oneshot service and 60-second monotonic timer.
It never writes unit files, calls Docker, grants IAM, starts services, or
enables monitoring. It refuses inconsistent tenant/Compose/GCP identities,
unsafe systemd arguments and privileged service usernames. The service runs
the existing fixed, private loopback sampler; it does not create public health
endpoints or accept secrets. Each run publishes **both** gateway and worker
readiness signals; enable Cloud Monitoring policies only for components
actually deployed and after confirming initialized metric series.

**Host privilege boundary:** membership in the `docker` group is effectively
root-equivalent even with systemd sandbox settings. The named `mg-monitor`
account, Docker socket access, pinned checkout path and host controls require
client-owner and security approval. Never install this bundle on an untrusted
host or assume `NoNewPrivileges` removes Docker daemon privileges.
Before an authorized install, review the generated text, check its fixed
`ExecStart`, verify the host's Python/Docker and GCE metricWriter identity,
run `systemd-analyze verify` on the staged units, and check the initial two
samples. Install the reviewed files as root-owned systemd units only through
the approved change process, then explicitly enable the timer. Record the
owner, rollback/retirement plan, alert recipients and actual failure/recovery
notification delivery. A stopped sampler produces missing telemetry rather
than a successful health report; Cloud Monitoring may require an initialized
series to alert on missing data. Unit rendering and offline tests do **not**
satisfy the live #231 acceptance drill.


## Optional private voice timer in the reviewed bundle (v0.8.237; #231)

For a client whose **voice Compose service is actually provisioned**, add
`--include-voice` to the reviewed `render-runtime-health-units.py` invocation
above. The same explicitly confirmed client/environment, Compose project,
GCP project and dedicated monitor user produce a **second** oneshot service
and 60-second timer. It invokes the existing
`scripts/voice-health-metric.py`, which samples the LiveKit container's
recent **successful** native Docker healthcheck (the last probe must have
integer exit code zero, not just cached `Status=healthy`) and publishes a boolean
`custom.googleapis.com/model_garden/voice_ready` through the VM's existing
GCE metricWriter identity. The gateway/worker timer remains unchanged.
Without the flag the bundle emits only gateway and worker units, so a
client with no voice overlay never reports a missing voice container as an
incident. Neither timer is installed or enabled by rendering.

A named operator must review **both** services, Docker root-equivalent
access, script provenance, systemd verification and the approved host
change before enabling either timer. First confirm real healthy and failed
voice samples and the exact client/environment/instance labels. Only then
review and apply `voice_private_alert_enabled=true` with the existing
client-authorized `gateway_uptime_notification_channels`. Keep the alert
disabled for clients without voice. Its configured OPENED incident is not
proof of actual recipient delivery; recovery needs a separately verified
healthy sample and operator acknowledgement. A healthy native LiveKit
worker is **not** proof of SIP connectivity, interrupted calls or transfer
success. Record an authorized voice failure/recovery and recipient drill
before closing #231. On retirement, disable the Terraform alert before
removing the host timer.
