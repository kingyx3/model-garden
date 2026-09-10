terraform {
  required_version = ">= 1.10.0"

  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "${var.client_slug}-${var.environment}-model-garden"
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
  tags = {
    Project     = "model-garden"
    Client      = var.client_slug
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = local.name
  cidr = "10.20.0.0/16"
  azs  = local.azs

  private_subnets = ["10.20.1.0/24", "10.20.2.0/24"]
  public_subnets  = ["10.20.101.0/24", "10.20.102.0/24"]

  enable_nat_gateway = true
  single_nat_gateway = true
  enable_dns_hostnames = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = local.name
  kubernetes_version = var.kubernetes_version

  endpoint_public_access                   = true
  enable_cluster_creator_admin_permissions = true

  addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni    = {}
  }

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  eks_managed_node_groups = {
    default = {
      instance_types = [var.node_instance_type]
      min_size       = 1
      max_size       = max(var.node_count, 3)
      desired_size   = var.node_count
    }
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

variable "region" {
  type        = string
  description = "AWS region."
}

variable "node_count" {
  type        = number
  description = "Desired EKS worker count."
  default     = 2
}

variable "node_instance_type" {
  type        = string
  description = "EKS worker instance type."
  default     = "m6i.large"
}

variable "kubernetes_version" {
  type        = string
  description = "EKS Kubernetes version."
  default     = "1.33"
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}
