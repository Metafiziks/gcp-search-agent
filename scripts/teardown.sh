#!/usr/bin/env bash
set -euo pipefail

# Protected mode (KB_ACCESS_MODE=protected): document-level access control, see guides/protected-mode.md
if [ "${KB_ACCESS_MODE:-sample}" = "protected" ]; then
  exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/teardown_protected.sh"
fi

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID env var}"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"

echo ""
echo "=== Teardown: Removing all resources ==="
echo ""
read -r -p "  ⚠️  This will delete all resources for ENV_NAME='${ENV_NAME}'. Press Enter to continue or Ctrl+C to cancel..."
echo ""

# --- Delete Cloud Run service (deployed by ADK, not tracked by Terraform) ---
echo "► Deleting Cloud Run service..."
gcloud run services delete "${ENV_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --quiet 2>/dev/null && echo "  ✓ Cloud Run service deleted" || echo "  ℹ  Cloud Run service not found (skipping)"
echo ""

# --- Terraform destroy ---
echo "► Destroying infrastructure with Terraform..."
terraform -chdir=terraform destroy -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}" \
  -var="github_repo=${GITHUB_REPO:-placeholder/placeholder}"
echo "  ✓ Infrastructure destroyed"
echo ""

echo "=== Teardown Complete ==="
echo ""
