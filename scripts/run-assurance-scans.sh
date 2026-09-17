#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${ROOT}/.enterprise-evidence/scans}"
PACK_DIR="$(dirname "${OUT}")"
mkdir -p "${OUT}"
cd "${ROOT}"

if ! command -v pip-audit >/dev/null 2>&1 || ! command -v bandit >/dev/null 2>&1 || ! command -v checkov >/dev/null 2>&1 || ! command -v detect-secrets >/dev/null 2>&1; then
  echo "Assurance scanner dependencies are missing. Install with:" >&2
  echo "  python3 -m pip install -r requirements-assurance.txt" >&2
  exit 2
fi

run_report() {
  local name="$1"
  shift
  echo "==> ${name}" >&2
  set +e
  "$@"
  local rc=$?
  set -e
  printf '%s\n' "${rc}" > "${OUT}/${name}.exit-code"
  if [[ ${rc} -ne 0 ]]; then
    echo "${name} reported findings or could not complete (exit ${rc}); evidence generation will preserve the result for review." >&2
  fi
}

# Test fixtures intentionally contain deterministic fake credentials. The scanner's own
# implementation files are excluded to avoid self-referential keyword detections. The
# pinned container digest is a narrow line-level allowlist. These scope decisions are
# recorded in assurance/evidence-metadata.yaml and copied into the generated artifact.
run_report detect-secrets detect-secrets scan --all-files --no-verify \
  --exclude-files '(^|/)\.git/' \
  --exclude-files '(^|/)\.enterprise-evidence/' \
  --exclude-files '(^|/)\.venv/' \
  --exclude-files '(^|/)venv/' \
  --exclude-files '(^|/)tests/' \
  --exclude-files '(^|/)scripts/(run-assurance-scans|enterprise-evidence)\.(sh|py)$' \
  --exclude-lines 'BASE_IMAGE = "python:' > "${OUT}/detect-secrets.json"

run_report pip-audit pip-audit -r requirements-dev.txt -r requirements-voice.txt --format json --output "${OUT}/pip-audit.json"
# Medium/high severity findings are the enterprise evidence threshold. B104 and B310
# are documented narrow exceptions: private Docker-network binding and validated HTTP(S)
# transport respectively. Their exact rationale is retained with the artifact metadata.
run_report bandit bandit -r scripts platform -ll --skip B104,B310 -f json -o "${OUT}/bandit.json"

set +e
checkov -d infra --framework terraform --output json --quiet > "${OUT}/checkov.json"
checkov_rc=$?
set -e
printf '%s\n' "${checkov_rc}" > "${OUT}/checkov.exit-code"
if [[ ${checkov_rc} -ne 0 ]]; then
  echo "checkov reported findings or could not complete (exit ${checkov_rc}); evidence generation will preserve the result for review." >&2
fi

# pip-audit can emit a CycloneDX SBOM while retaining vulnerability information.
set +e
pip-audit -r requirements-dev.txt -r requirements-voice.txt --format cyclonedx-json --output "${OUT}/python-sbom.cdx.json"
sbom_rc=$?
set -e
printf '%s\n' "${sbom_rc}" > "${OUT}/python-sbom.exit-code"
if [[ ${sbom_rc} -ne 0 ]]; then
  echo "SBOM generation completed with vulnerability findings or an error (exit ${sbom_rc})." >&2
fi

python3 scripts/enterprise-evidence.py --scan-dir "${OUT}" --output-dir "${PACK_DIR}"
cp assurance/evidence-metadata.yaml "${PACK_DIR}/evidence-metadata.yaml"
