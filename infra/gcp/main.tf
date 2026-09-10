terraform {
  required_version = ">= 1.10.0"

  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  name = "${var.client_slug}-${var.environment}-model-garden"
  zone = coalesce(var.zone, "${var.region}-a")
  labels = {
    project     = "model-garden"
    client      = var.client_slug
    environment = var.environment
    managed_by  = "terraform"
  }
}

resource "google_project_service" "services" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com"
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "main" {
  name                    = local.name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.services]
}

resource "google_compute_subnetwork" "main" {
  name          = local.name
  region        = var.region
  network       = google_compute_network.main.id
  ip_cidr_range = "10.30.0.0/20"

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.40.0.0/14"
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.44.0.0/20"
  }
}

resource "google_container_cluster" "main" {
  name     = local.name
  location = local.zone

  network    = google_compute_network.main.id
  subnetwork = google_compute_subnetwork.main.id

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false
  networking_mode          = "VPC_NATIVE"

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  resource_labels = local.labels

  depends_on = [google_project_service.services]
}

resource "google_container_node_pool" "default" {
  name       = "default"
  location   = local.zone
  cluster    = google_container_cluster.main.name
  node_count = var.node_count

  node_config {
    machine_type = var.node_machine_type
    disk_size_gb = 50
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    labels = local.labels
  }
}

variable "client_slug" {
  type        = string
  description = "Short lowercase client identifier used in resource names."
  default     = "client"
}

variable "environment" {
  type        = string
  description = "Deployment environment."
  default     = "dev"
}

variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "region" {
  type        = string
  description = "GCP region."
}

variable "zone" {
  type        = string
  description = "Optional GKE zone; defaults to <region>-a."
  default     = null
  nullable    = true
}

variable "node_count" {
  type        = number
  description = "Desired GKE worker count."
  default     = 2
}

variable "node_machine_type" {
  type        = string
  description = "GKE worker machine type."
  default     = "e2-standard-4"
}

output "cluster_name" {
  value = google_container_cluster.main.name
}

output "location" {
  value = local.zone
}

output "project_id" {
  value = var.project_id
}
