# Optional client gateway uptime alert (#231)

The Terraform in `infra/docker-host/gcp/gateway-uptime.tf` is disabled by default. It reuses the existing HTTPS load balancer and Cloud Monitoring. It does not grant new IAM roles, expose a private VM, or create notification channels.

An authorized client operator must verify HTTPS DNS, TLS, and `/healthz`, and supply an **existing approved channel in the same GCP project**. Only then set `gateway_ingress_enabled=true`, `gateway_domain`, `gateway_uptime_alert_enabled=true`, and `gateway_uptime_notification_channels`. Never apply to a live client without authorization.

A 60-second TLS-validated uptime check targets `/healthz`; the alert threshold is 180 seconds, with requested incident-open and incident-close notifications. Cloud Monitoring owns notification delivery and deduplication. Operators should inspect Cloud Monitoring, DNS/TLS and the gateway, then use approved private-host access to inspect workers and escalate to the client owner. Do not paste customer payloads or secrets into incidents.

**Coverage and evidence:** This is gateway-only, not worker-heartbeat or voice-path monitoring. Neither Terraform plan/apply nor a live notification drill has been proven here. Capture authorized failure/recovery notifications and delivery times before claiming operational readiness. Keep #231 open for worker/voice and live evidence.
