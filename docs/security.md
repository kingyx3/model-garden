# Production hardening

The default deployment is intentionally simple enough for a client proof-of-value. Treat it as a baseline, not a finished security architecture.

Before production:

1. Put the gateway behind enterprise ingress/API management with TLS, SSO, WAF/rate limits, and private connectivity where appropriate.
2. Replace direct Kubernetes secrets with the cloud secret manager plus External Secrets/CSI integration and rotation.
3. Make cluster control planes private or tightly allow-listed; use self-hosted runners when a private API endpoint is required.
4. Add Kubernetes NetworkPolicies and namespace/workload identities. Do not share cloud admin credentials with workloads.
5. Export LiteLLM/application audit events and Kubernetes/cloud audit logs into the client's SIEM.
6. Add persistent gateway metadata (managed PostgreSQL), budgets/quotas, per-team virtual keys, and chargeback tags.
7. Add an explicit policy/evaluation gate before registering a new model or increasing agent autonomy.
8. Pin and regularly update container/action/module versions; enable dependency and image scanning.
9. Restrict open-weight endpoints to private networking and verify model licenses before enterprise use.
10. Use protected GitHub Environments with required reviewers for production applies.

## Terraform state

`scripts/bootstrap-state.sh` creates encrypted, versioned remote state storage in the selected cloud. Limit state access tightly: state can contain infrastructure metadata and generated credentials if you add sensitive resources later.

## Threat boundary

The gateway is the trust boundary between enterprise applications and model providers. Applications should receive a company gateway credential/identity, not raw provider credentials. Downstream agent/tool access should be separately authorized; model access does not imply permission to act on enterprise systems.
