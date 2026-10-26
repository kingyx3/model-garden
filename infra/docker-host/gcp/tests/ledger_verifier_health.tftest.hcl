# No external cloud mutations; independent verifier monitoring remains opt-in.
mock_provider "google" {}

variables {
  project_id              = "sample-project-123"
  region                  = "asia-southeast1"
  zone                    = "asia-southeast1-a"
  client_slug             = "acme"
  environment             = "dev"
  platform_version        = "0.8.146"
  runtime_service_account = "runtime@sample-project-123.iam.gserviceaccount.com"
}

run "disabled_by_default" {
  command = plan
  assert {
    condition     = length(google_monitoring_alert_policy.ledger_verifier) == 0 && length(google_logging_metric.ledger_verifier_unhealthy) == 0
    error_message = "Independent verifier monitoring must be opt-in."
  }
}

run "rejects_missing_identity_or_channels" {
  command = plan
  variables {
    ledger_verifier_alert_enabled = true
  }
  expect_failures = [var.ledger_verifier_alert_enabled]
}

run "approved_private_verifier_alert" {
  command = plan
  variables {
    ledger_export_enabled                = true
    ledger_verifier_alert_enabled        = true
    export_verifier_members              = ["serviceAccount:auditor@sample-project-123.iam.gserviceaccount.com"]
    gateway_uptime_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
  }
  assert {
    condition     = length(google_monitoring_alert_policy.ledger_verifier) == 1 && length(google_logging_metric.ledger_verifier_heartbeat) == 1
    error_message = "Independent verifier must have both health and missing-sample detection."
  }
  assert {
    condition     = google_monitoring_alert_policy.ledger_verifier[0].notification_channels == var.gateway_uptime_notification_channels
    error_message = "Verifier alerts must use approved client notification channels."
  }
  assert {
    condition     = google_monitoring_alert_policy.ledger_verifier[0].conditions[1].condition_absent[0].duration == "900s"
    error_message = "Missing verifier heartbeat must be detected."
  }
  assert {
    condition     = strcontains(google_logging_metric.ledger_verifier_unhealthy[0].filter, "jsonPayload.clientProject=\"acme-dev\"")
    error_message = "Unhealthy signals must be scoped to the exact client environment."
  }
}
