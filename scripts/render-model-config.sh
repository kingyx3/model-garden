#!/usr/bin/env bash
set -euo pipefail

out="${1:-/tmp/litellm-config.yaml}"
models=""

append_model() {
  local alias="$1"
  local model_ref="$2"
  local extra="$3"
  models+="  - model_name: ${alias}\n    litellm_params:\n      model: ${model_ref}\n${extra}"
}

if [[ -n "${OPENAI_API_KEY:-}" && -n "${OPENAI_MODEL_REF:-}" ]]; then
  append_model "general.openai" "$OPENAI_MODEL_REF" "      api_key: os.environ/OPENAI_API_KEY\n"
fi

if [[ -n "${ANTHROPIC_API_KEY:-}" && -n "${ANTHROPIC_MODEL_REF:-}" ]]; then
  append_model "general.anthropic" "$ANTHROPIC_MODEL_REF" "      api_key: os.environ/ANTHROPIC_API_KEY\n"
fi

if [[ -n "${OPEN_WEIGHT_API_BASE:-}" && -n "${OPEN_WEIGHT_MODEL_REF:-}" ]]; then
  append_model "general.open-weight" "$OPEN_WEIGHT_MODEL_REF" "      api_base: os.environ/OPEN_WEIGHT_API_BASE\n      api_key: os.environ/OPEN_WEIGHT_API_KEY\n"
fi

if [[ -z "$models" ]]; then
  echo "No model provider is fully configured. See docs/secrets.md." >&2
  exit 1
fi

{
  printf 'model_list:\n%b' "$models"
  cat <<'EOF'

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY

litellm_settings:
  drop_params: true
EOF
} >"$out"

echo "Rendered model aliases:"
grep 'model_name:' "$out" | sed 's/^/ -/'
