#!/usr/bin/env bash
# Protected mode teardown (KB_ACCESS_MODE=protected). Called from teardown.sh.
set -euo pipefail

STATE_FILE="${PROTECTED_STATE_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.protected-project}"
PROJECT_ID="${PROJECT_ID:-$(cat "${STATE_FILE}" 2>/dev/null || true)}"
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID env var}"
CREATED_BY_LABEL="gcp-search-agent-protected"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/../terraform/protected"

echo ""
echo "=== Teardown (protected): removing the protected environment '${ENV_NAME}-protected' ==="
if [ "${ASSUME_YES:-0}" != "1" ]; then
  read -r -p "  ⚠️  Deletes the service, private bucket (and its documents), data store and IAM. Enter to continue, Ctrl+C to cancel..."
fi
echo ""

echo "► Deleting Cloud Run service..."
gcloud run services delete "${ENV_NAME}-protected" --region "${REGION}" --project "${PROJECT_ID}" --quiet 2>/dev/null \
  && echo "  ✓ Cloud Run service deleted" || echo "  ℹ  Cloud Run service not found (skipping)"

# `gcloud run deploy --source` stores uploads and the built image outside Terraform. Remove only this
# service's copies; the shared run-sources bucket and cloud-run-source-deploy repo may serve other apps.
SERVICE="${ENV_NAME}-protected"
gcloud storage rm -r "gs://run-sources-${PROJECT_ID}-${REGION}/services/${SERVICE}/" --project "${PROJECT_ID}" --quiet 2>/dev/null \
  && echo "  ✓ Source uploads for ${SERVICE} deleted" || echo "  ℹ  No source uploads for ${SERVICE} (skipping)"
gcloud artifacts docker images delete "${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}" \
  --delete-tags --project "${PROJECT_ID}" --quiet 2>/dev/null \
  && echo "  ✓ Container image for ${SERVICE} deleted" || echo "  ℹ  No container image for ${SERVICE} (skipping)"

echo "► Destroying protected infrastructure with Terraform..."
terraform -chdir="${TF_DIR}" destroy -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}" \
  -var="manage_acl_config=${MANAGE_ACL_CONFIG:-true}" \
  -var="idp_type=${IDP_TYPE:-GSUITE}" \
  -var="workforce_pool_name=${WORKFORCE_POOL_NAME:-}"
echo "  ✓ Infrastructure destroyed"
echo ""
# --- Optional: delete the project (DELETE_PROJECT=1) -------------------------------------------
# Only a project that provision.sh created (CREATE_PROJECT=1) and labelled, and only when nothing
# else runs in it. Deletion is restorable for 30 days (gcloud projects undelete).
if [ "${DELETE_PROJECT:-0}" = "1" ]; then
  LABEL="$(gcloud projects describe "${PROJECT_ID}" --format='value(labels.created-by)' 2>/dev/null || true)"
  if [ "${LABEL}" != "${CREATED_BY_LABEL}" ]; then
    echo "ERROR: ${PROJECT_ID} was not created by this tool (missing label created-by=${CREATED_BY_LABEL}); refusing to delete it." >&2
    exit 1
  fi
  OTHERS="$(gcloud run services list --project "${PROJECT_ID}" --format='value(metadata.name)' 2>/dev/null || true)"
  if [ -n "${OTHERS}" ]; then
    echo "ERROR: ${PROJECT_ID} still has Cloud Run services (${OTHERS//$'\n'/, }); refusing to delete it." >&2
    exit 1
  fi
  if [ "${ASSUME_YES:-0}" != "1" ]; then
    read -r -p "  Type the project ID (${PROJECT_ID}) to DELETE the whole project: " CONFIRM
    [ "${CONFIRM}" = "${PROJECT_ID}" ] || { echo "ERROR: project ID did not match; not deleting." >&2; exit 1; }
  fi
  gcloud projects delete "${PROJECT_ID}" --quiet
  rm -f "${STATE_FILE}"
  echo "  ✓ Project ${PROJECT_ID} scheduled for deletion (restore within 30 days: gcloud projects undelete ${PROJECT_ID})"
fi

echo "=== Teardown Complete (protected) ==="
echo "  Check separately created test users, groups and IAM bindings; this script only removes what it created."
echo ""
