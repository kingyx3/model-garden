#!/usr/bin/env bash
set -euo pipefail

cloud="${1:?usage: bootstrap-state.sh <aws|gcp|azure>}"
out="${2:-/tmp/backend.hcl}"
client_slug="${CLIENT_SLUG:-client}"
environment="${ENVIRONMENT:-dev}"
state_key="${client_slug}/${environment}/terraform.tfstate"

case "$cloud" in
  aws)
    : "${AWS_REGION:?AWS_REGION is required}"
    account_id="$(aws sts get-caller-identity --query Account --output text)"
    bucket="model-garden-tfstate-${account_id}-${AWS_REGION}"

    if ! aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
      if [[ "$AWS_REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$bucket" >/dev/null
      else
        aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION" \
          --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
      fi
    fi

    aws s3api put-bucket-encryption --bucket "$bucket" \
      --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
    aws s3api put-bucket-versioning --bucket "$bucket" \
      --versioning-configuration Status=Enabled >/dev/null
    aws s3api put-public-access-block --bucket "$bucket" \
      --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null

    cat >"$out" <<EOF
bucket       = "$bucket"
key          = "$state_key"
region       = "$AWS_REGION"
encrypt      = true
use_lockfile = true
EOF
    ;;

  gcp)
    : "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
    : "${GCP_REGION:?GCP_REGION is required}"
    bucket="model-garden-tfstate-${GCP_PROJECT_ID}"

    if ! gcloud storage buckets describe "gs://$bucket" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
      gcloud storage buckets create "gs://$bucket" \
        --project "$GCP_PROJECT_ID" --location "$GCP_REGION" --uniform-bucket-level-access >/dev/null
    fi
    gcloud storage buckets update "gs://$bucket" --versioning >/dev/null

    cat >"$out" <<EOF
bucket = "$bucket"
prefix = "$state_key"
EOF
    ;;

  azure)
    : "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID is required}"
    : "${AZURE_LOCATION:?AZURE_LOCATION is required}"
    compact_sub="${AZURE_SUBSCRIPTION_ID//-/}"
    storage_account="mgstate${compact_sub:0:16}"
    resource_group="${client_slug}-model-garden-tfstate"
    container="tfstate"

    az group create --name "$resource_group" --location "$AZURE_LOCATION" --output none
    if ! az storage account show --resource-group "$resource_group" --name "$storage_account" >/dev/null 2>&1; then
      az storage account create --resource-group "$resource_group" --name "$storage_account" \
        --location "$AZURE_LOCATION" --sku Standard_LRS --kind StorageV2 \
        --min-tls-version TLS1_2 --allow-blob-public-access false --output none
    fi
    az storage container create --name "$container" --account-name "$storage_account" --auth-mode login --output none

    cat >"$out" <<EOF
resource_group_name  = "$resource_group"
storage_account_name = "$storage_account"
container_name       = "$container"
key                  = "$state_key"
use_cli              = true
EOF
    ;;

  *)
    echo "unsupported cloud: $cloud" >&2
    exit 2
    ;;
esac

echo "backend config written to $out"
