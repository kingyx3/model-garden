# Credential-free, opt-in worker readiness monitoring (#231).
mock_provider "google" {}

variables {
  project_id              = "sample-project-123"
  region                  = "asia-southeast1"
  zone                    = "asia-southeast1-a"
  client_slug             = "acme"
  environment             = "dev"
  platform_version        = "0.8.109"
  runtime_service_account = "runtime@sample-project-123.iam.gserviceaccount.com"
}

run "worker_monitoring_disabled_by_default" {
  command = plan
  assert {
    condition     = length(google_monitoring_uptime_check_config.worker) == 0 && length(google_monitoring_alert_policy.worker_uptime) == 0
    error_message = "Worker monitoring must never activate without explicit opt-in."
  }
}

run "reject_worker_monitoring_without_gateway_authorization" {
  command = plan
  variables {
    worker_uptime_alert_enabled = true
  }
  expect_failures = [var.worker_uptime_alert_enabled]
}

run "reject_worker_monitoring_without_authorized_channels" {
  command = plan
  variables {
    gateway_ingress_enabled      = true
    gateway_domain               = "gateway.example.com"
    gateway_uptime_alert_enabled = true
    worker_uptime_alert_enabled  = true
  }
  expect_failures = [var.gateway_uptime_alert_enabled]
}

run "valid_worker_readiness_monitor" {
  command = plan
  variables {
    gateway_ingress_enabled              = true
    gateway_domain                       = "gateway.example.com"
    gateway_uptime_alert_enabled         = true
    gateway_uptime_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
    worker_uptime_alert_enabled          = true
  }
  assert {
    condition     = length(google_monitoring_uptime_check_config.worker) == 1 && length(google_monitoring_alert_policy.worker_uptime) == 1
    error_message = "Exactly one worker uptime check and alert policy must exist."
  }
  assert {
    condition     = google_monitoring_uptime_check_config.worker[0].http_check[0].path == "/health/worker" && google_monitoring_uptime_check_config.worker[0].http_check[0].validate_ssl == true
    error_message = "Worker check must use the payload-free HTTPS readiness route with TLS verification."
  }
  assert {
    condition     = google_monitoring_uptime_check_config.worker[0].monitored_resource[0].labels["project_id"] == var.project_id && google_monitoring_uptime_check_config.worker[0].monitored_resource[0].labels["host"] == var.gateway_domain
    error_message = "Worker check must target only the authorized client ingress."
  }
  assert {
    condition     = google_monitoring_alert_policy.worker_uptime[0].notification_channels == var.gateway_uptime_notification_channels && google_monitoring_alert_policy.worker_uptime[0].conditions[0].condition_threshold[0].evaluation_missing_data == "EVALUATION_MISSING_DATA_ACTIVE"
    error_message = "Worker incidents must reuse the approved channels and fail closed on missing telemetry."
  }
  assert {
    condition     = google_monitoring_alert_policy.worker_uptime[0].alert_strategy[0].notification_prompts == ["OPENED"]
    error_message = "Do not mistake an automatically closed worker incident for verified recovery."
  }
}
