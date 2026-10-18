# Public reachability for the channel gateway (#198): a global external HTTPS load balancer in front of the VM's gateway container.
#
# Opt-in (gateway_ingress_enabled, off by default). The VM keeps no external IP. Slack, Teams and browsers reach the gateway through
# the load balancer, which terminates TLS with a Google-managed certificate for gateway_domain and forwards to tcp:8080. The only
# new inbound path to the VM is a firewall rule for Google's front-end and health-check ranges. The binding must publish the
# gateway on the VM interface (gateway.bind: 0.0.0.0); that firewall, not the bind address, is what keeps the port off the internet.
#
# Optional inbound mail (gateway_smtp_enabled) uses a global TCP proxy load balancer so the public port is 25 while the container
# listens on 2525. Because the proxy hides the sender, the source allow-list is a Cloud Armor policy, not a VM firewall rule.

locals {
  ingress      = var.gateway_ingress_enabled ? 1 : 0
  smtp         = var.gateway_ingress_enabled && var.gateway_smtp_enabled ? 1 : 0
  dns          = var.gateway_ingress_enabled && var.gateway_dns_zone != "" ? 1 : 0
  google_lb_ip = ["130.211.0.0/22", "35.191.0.0/16"]
}

resource "google_compute_instance_group" "gateway" {
  count = local.ingress

  name      = "${local.name}-gateway"
  zone      = var.zone
  network   = google_compute_network.main.id
  instances = [google_compute_instance.runtime.self_link]

  named_port {
    name = "gateway"
    port = 8080
  }

  named_port {
    name = "smtp"
    port = 2525
  }
}

resource "google_compute_firewall" "gateway_lb" {
  count = local.ingress

  name    = "${local.name}-gateway-lb"
  network = google_compute_network.main.name

  direction     = "INGRESS"
  source_ranges = local.google_lb_ip
  target_tags   = ["model-garden-runtime"]

  allow {
    protocol = "tcp"
    ports    = var.gateway_smtp_enabled ? ["8080", "2525"] : ["8080"]
  }
}

resource "google_compute_global_address" "gateway" {
  count = local.ingress

  name = "${local.name}-gateway"
}

resource "google_compute_managed_ssl_certificate" "gateway" {
  count = local.ingress

  name = "${local.name}-gateway"

  managed {
    domains = [var.gateway_domain]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_ssl_policy" "gateway" {
  count = local.ingress

  name            = "${local.name}-gateway"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

resource "google_compute_health_check" "gateway" {
  count = local.ingress

  name                = "${local.name}-gateway-http"
  check_interval_sec  = 10
  timeout_sec         = 5
  healthy_threshold   = 2
  unhealthy_threshold = 3

  http_health_check {
    port         = 8080
    request_path = "/healthz"
  }
}

resource "google_compute_backend_service" "gateway" {
  count = local.ingress

  name                  = "${local.name}-gateway"
  protocol              = "HTTP"
  port_name             = "gateway"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 30
  health_checks         = [google_compute_health_check.gateway[0].id]

  backend {
    group           = google_compute_instance_group.gateway[0].id
    balancing_mode  = "UTILIZATION"
    capacity_scaler = 1.0
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

resource "google_compute_url_map" "gateway" {
  count = local.ingress

  name            = "${local.name}-gateway"
  default_service = google_compute_backend_service.gateway[0].id
}

resource "google_compute_target_https_proxy" "gateway" {
  count = local.ingress

  name             = "${local.name}-gateway"
  url_map          = google_compute_url_map.gateway[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.gateway[0].id]
  ssl_policy       = google_compute_ssl_policy.gateway[0].id
}

resource "google_compute_global_forwarding_rule" "gateway_https" {
  count = local.ingress

  name                  = "${local.name}-gateway-https"
  ip_address            = google_compute_global_address.gateway[0].id
  ip_protocol           = "TCP"
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.gateway[0].id
}

# Plain HTTP only redirects to HTTPS; it never reaches the backend.
resource "google_compute_url_map" "gateway_redirect" {
  count = local.ingress

  name = "${local.name}-gateway-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "gateway_redirect" {
  count = local.ingress

  name    = "${local.name}-gateway-redirect"
  url_map = google_compute_url_map.gateway_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "gateway_http" {
  count = local.ingress

  name                  = "${local.name}-gateway-http"
  ip_address            = google_compute_global_address.gateway[0].id
  ip_protocol           = "TCP"
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_http_proxy.gateway_redirect[0].id
}

data "google_dns_managed_zone" "gateway" {
  count = local.dns

  name = var.gateway_dns_zone
}

resource "google_dns_record_set" "gateway" {
  count = local.dns

  managed_zone = data.google_dns_managed_zone.gateway[0].name
  name         = "${var.gateway_domain}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.gateway[0].address]
}

# Optional inbound mail: public tcp:25 -> container tcp:2525, only from gateway_smtp_source_ranges.
resource "google_compute_security_policy" "gateway_smtp" {
  count = local.smtp

  name = "${local.name}-gateway-smtp"

  rule {
    action      = "allow"
    priority    = 1000
    description = "Configured mail sources"

    match {
      versioned_expr = "SRC_IPS_V1"

      config {
        src_ip_ranges = var.gateway_smtp_source_ranges
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 2147483647
    description = "Default deny"

    match {
      versioned_expr = "SRC_IPS_V1"

      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_health_check" "gateway_smtp" {
  count = local.smtp

  name                = "${local.name}-gateway-smtp"
  check_interval_sec  = 10
  timeout_sec         = 5
  healthy_threshold   = 2
  unhealthy_threshold = 3

  tcp_health_check {
    port = 2525
  }
}

resource "google_compute_backend_service" "gateway_smtp" {
  count = local.smtp

  name                  = "${local.name}-gateway-smtp"
  protocol              = "TCP"
  port_name             = "smtp"
  load_balancing_scheme = "EXTERNAL"
  timeout_sec           = 300
  health_checks         = [google_compute_health_check.gateway_smtp[0].id]
  security_policy       = google_compute_security_policy.gateway_smtp[0].id

  backend {
    group           = google_compute_instance_group.gateway[0].id
    balancing_mode  = "UTILIZATION"
    capacity_scaler = 1.0
  }
}

resource "google_compute_target_tcp_proxy" "gateway_smtp" {
  count = local.smtp

  name            = "${local.name}-gateway-smtp"
  backend_service = google_compute_backend_service.gateway_smtp[0].id
}

resource "google_compute_global_address" "gateway_smtp" {
  count = local.smtp

  name = "${local.name}-gateway-smtp"
}

resource "google_compute_global_forwarding_rule" "gateway_smtp" {
  count = local.smtp

  name                  = "${local.name}-gateway-smtp"
  ip_address            = google_compute_global_address.gateway_smtp[0].id
  ip_protocol           = "TCP"
  port_range            = "25"
  load_balancing_scheme = "EXTERNAL"
  target                = google_compute_target_tcp_proxy.gateway_smtp[0].id
}

variable "gateway_ingress_enabled" {
  type        = bool
  description = "Create a global external HTTPS load balancer in front of the channel gateway (port 8080). Requires gateway_domain."
  default     = false
}

variable "gateway_domain" {
  type        = string
  description = "Public hostname for the gateway, for example chat.client.example. Gets a Google-managed certificate; DNS must point at gateway_public_ip."
  default     = ""

  validation {
    condition     = var.gateway_domain == "" || can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.gateway_domain))
    error_message = "gateway_domain must be a lowercase fully qualified hostname such as chat.client.example."
  }

  validation {
    condition     = !var.gateway_ingress_enabled || var.gateway_domain != ""
    error_message = "gateway_domain is required when gateway_ingress_enabled is true."
  }
}

variable "gateway_dns_zone" {
  type        = string
  description = "Optional Cloud DNS managed zone name in this project. When set, an A record for gateway_domain is created; otherwise point DNS at gateway_public_ip yourself."
  default     = ""

  validation {
    condition     = var.gateway_dns_zone == "" || can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", var.gateway_dns_zone))
    error_message = "gateway_dns_zone must be a Cloud DNS managed zone name (lowercase letters, digits, hyphens), not a DNS name."
  }
}

variable "gateway_smtp_enabled" {
  type        = bool
  description = "Also expose the gateway SMTP listener (public port 25 to container port 2525), restricted to gateway_smtp_source_ranges. Requires gateway_ingress_enabled."
  default     = false

  validation {
    condition     = !var.gateway_smtp_enabled || var.gateway_ingress_enabled
    error_message = "gateway_smtp_enabled requires gateway_ingress_enabled."
  }
}

variable "gateway_smtp_source_ranges" {
  type        = list(string)
  description = "CIDR ranges allowed to deliver mail (at most 10). Required when gateway_smtp_enabled; never 0.0.0.0/0."
  default     = []

  validation {
    condition     = !var.gateway_smtp_enabled || length(var.gateway_smtp_source_ranges) > 0
    error_message = "gateway_smtp_source_ranges must be non-empty when gateway_smtp_enabled is true."
  }

  validation {
    condition     = length(var.gateway_smtp_source_ranges) <= 10
    error_message = "gateway_smtp_source_ranges allows at most 10 ranges."
  }

  validation {
    condition = alltrue([
      for cidr in var.gateway_smtp_source_ranges :
      can(cidrhost(cidr, 0)) && can(regex("/([1-9]|[12][0-9]|3[0-2])$", cidr))
    ])
    error_message = "gateway_smtp_source_ranges must be valid IPv4 CIDRs with a prefix of /1 or longer; 0.0.0.0/0 is not allowed."
  }
}

output "gateway_public_ip" {
  description = "Point the gateway_domain A record here (null when ingress is disabled)."
  value       = try(google_compute_global_address.gateway[0].address, null)
}

output "gateway_url" {
  description = "Public HTTPS origin of the gateway; use it as gateway.public_origin (null when ingress is disabled)."
  value       = var.gateway_ingress_enabled ? "https://${var.gateway_domain}" : null
}

output "gateway_smtp_ip" {
  description = "Public IP for the mail host's DNS record (null unless gateway_smtp_enabled)."
  value       = try(google_compute_global_address.gateway_smtp[0].address, null)
}
