mock_provider "google" {}

variables {
  project_id              = "sample-project-123"
  region                  = "asia-southeast1"
  zone                    = "asia-southeast1-a"
  client_slug             = "acme"
  environment             = "dev"
  platform_version        = "0.8.76"
  runtime_service_account = "runtime@sample-project-123.iam.gserviceaccount.com"
}

run "disabled_by_default" {
  command = plan
  assert {
    condition     = length(google_monitoring_uptime_check_config.gateway) == 0 && length(google_monitoring_alert_policy.gateway_uptime) == 0
    error_message = "No monitoring resources may exist without explicit opt-in."
  }
}

run "rejects_alert_without_ingress" {
  command = plan
  variables {
    gateway_uptime_alert_enabled                 = true
    gateway_legacy_public_readiness_acknowledged = true
    gateway_uptime_notification_channels         = ["projects/sample-project-123/notificationChannels/123"]
  }
  expect_failures = [var.gateway_uptime_alert_enabled]
}

run "rejects_missing_authorized_channel" {
  command = plan
  variables {
    gateway_ingress_enabled                      = true
    gateway_domain                               = "gateway.example.com"
    gateway_uptime_alert_enabled                 = true
    gateway_legacy_public_readiness_acknowledged = true
  }
  expect_failures = [var.gateway_uptime_alert_enabled]
}

run "rejects_cross_project_channel" {
  command = plan
  variables {
    gateway_ingress_enabled                      = true
    gateway_domain                               = "gateway.example.com"
    gateway_uptime_alert_enabled                 = true
    gateway_legacy_public_readiness_acknowledged = true
    gateway_uptime_notification_channels         = ["projects/other-project-123/notificationChannels/123"]
  }
  expect_failures = [var.gateway_uptime_notification_channels]
}

run "rejects_malformed_channel" {
  command = plan
  variables {
    gateway_ingress_enabled                      = true
    gateway_domain                               = "gateway.example.com"
    gateway_uptime_alert_enabled                 = true
    gateway_legacy_public_readiness_acknowledged = true
    gateway_uptime_notification_channels         = ["projects/sample-project-123/notificationChannels/not-numeric"]
  }
  expect_failures = [var.gateway_uptime_notification_channels]
}

run "rejects_mixed_project_channels" {
  command = plan
  variables {
    gateway_ingress_enabled                      = true
    gateway_domain                               = "gateway.example.com"
    gateway_uptime_alert_enabled                 = true
    gateway_legacy_public_readiness_acknowledged = true
    gateway_uptime_notification_channels         = ["projects/sample-project-123/notificationChannels/123", "projects/foreign-project-123/notificationChannels/999"]
  }
  expect_failures = [var.gateway_uptime_notification_channels]
}

run "rejects_unacknowledged_legacy_public_readiness" {
  command = plan
  variables {
    gateway_ingress_enabled              = true
    gateway_domain                       = "gateway.example.com"
    gateway_uptime_alert_enabled         = true
    gateway_uptime_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
  }
  expect_failures = [var.gateway_uptime_alert_enabled]
}

run "valid_gateway_uptime_alert" {
  command = plan
  variables {
    gateway_ingress_enabled                      = true
    gateway_domain                               = "gateway.example.com"
    gateway_uptime_alert_enabled                 = true
    gateway_legacy_public_readiness_acknowledged = true
    gateway_uptime_notification_channels         = ["projects/sample-project-123/notificationChannels/123"]
  }
  assert {
    condition     = length(google_monitoring_uptime_check_config.gateway) == 1 && length(google_monitoring_alert_policy.gateway_uptime) == 1
    error_message = "An opted-in client gateway must have exactly one check and one alert policy."
  }
  assert {
    condition     = google_monitoring_uptime_check_config.gateway[0].http_check[0].validate_ssl == true && google_monitoring_uptime_check_config.gateway[0].http_check[0].path == "/health/gateway"
    error_message = "Gateway monitoring must validate TLS and check the periodic loop, not just HTTP liveness."
  }
  assert {
    condition     = google_monitoring_alert_policy.gateway_uptime[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_LT" && google_monitoring_alert_policy.gateway_uptime[0].conditions[0].condition_threshold[0].threshold_value == 0.5 && google_monitoring_alert_policy.gateway_uptime[0].conditions[0].condition_threshold[0].duration == "180s"
    error_message = "Gateway degradation threshold and duration must remain explicit."
  }
  assert {
    condition     = google_monitoring_alert_policy.gateway_uptime[0].conditions[0].condition_threshold[0].aggregations[0].per_series_aligner == "ALIGN_FRACTION_TRUE" && google_monitoring_alert_policy.gateway_uptime[0].conditions[0].condition_threshold[0].aggregations[0].cross_series_reducer == "REDUCE_MEAN" && google_monitoring_alert_policy.gateway_uptime[0].conditions[0].condition_threshold[0].aggregations[0].alignment_period == "60s"
    error_message = "Aggregate the check pass fractions across locations before evaluating majority failure."
  }
  assert {
    condition     = google_monitoring_alert_policy.gateway_uptime[0].conditions[0].condition_threshold[0].evaluation_missing_data == "EVALUATION_MISSING_DATA_ACTIVE" && google_monitoring_alert_policy.gateway_uptime[0].alert_strategy[0].auto_close == "1800s"
    error_message = "Missing telemetry and auto-closure must remain explicit; closure is not recovery proof."
  }
  assert {
    condition     = google_monitoring_uptime_check_config.gateway[0].monitored_resource[0].labels["project_id"] == var.project_id && google_monitoring_uptime_check_config.gateway[0].monitored_resource[0].labels["host"] == var.gateway_domain && google_monitoring_alert_policy.gateway_uptime[0].notification_channels == var.gateway_uptime_notification_channels
    error_message = "Gateway monitoring must target only the supplied project, domain, and authorized channels."
  }
  assert {
    condition     = contains(google_monitoring_alert_policy.gateway_uptime[0].alert_strategy[0].notification_prompts, "OPENED") && contains(google_monitoring_alert_policy.gateway_uptime[0].alert_strategy[0].notification_prompts, "CLOSED")
    error_message = "Gateway incidents must request opening and closing notifications."
  }
}
