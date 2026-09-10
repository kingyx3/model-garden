terraform {
  required_version = ">= 1.10.0"

  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

locals {
  name = "${var.client_slug}-${var.environment}-model-garden"
  tags = {
    Project     = "model-garden"
    Client      = var.client_slug
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "azurerm_resource_group" "main" {
  name     = local.name
  location = var.location
  tags     = local.tags
}

resource "azurerm_virtual_network" "main" {
  name                = local.name
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.50.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "aks"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.50.0.0/20"]
}

resource "azurerm_kubernetes_cluster" "main" {
  name                = local.name
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  dns_prefix          = replace(local.name, "_", "-")

  role_based_access_control_enabled = true
  local_account_disabled            = false

  default_node_pool {
    name           = "system"
    node_count     = var.node_count
    vm_size        = var.node_vm_size
    vnet_subnet_id = azurerm_subnet.aks.id
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin = "azure"
    service_cidr   = "10.60.0.0/16"
    dns_service_ip = "10.60.0.10"
  }

  tags = local.tags
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

variable "subscription_id" {
  type        = string
  description = "Azure subscription ID."
}

variable "location" {
  type        = string
  description = "Azure region."
}

variable "node_count" {
  type        = number
  description = "Desired AKS worker count."
  default     = 2
}

variable "node_vm_size" {
  type        = string
  description = "AKS worker VM size."
  default     = "Standard_D4s_v5"
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.main.name
}

output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "location" {
  value = var.location
}
