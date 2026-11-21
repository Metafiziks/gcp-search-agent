#!/usr/bin/env bash
# Protected mode provisioning (KB_ACCESS_MODE=protected). Called from provision.sh.
# Creates a dedicated ACL-enabled data store, a private bucket and least-privilege IAM.
# It never uploads the public sample corpus, creates a public bucket, or uses API keys.
set -euo pipefail

ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/../terraform/protected"
# Remembers the project this script created so deploy.sh / teardown.sh can find it again.
STATE_FILE="${PROTECTED_STATE_FILE:-${SCRIPT_DIR}/../.protected-project}"
CREATED_BY_LABEL="gcp-search-agent-protected"

# Comma-separated IAM members -> JSON list for Terraform
json_list() { python3 -c 'import json,sys; print(json.dumps([m.strip() for m in sys.argv[1].split(",") if m.strip()]))' "${1:-}"; }

# --- Optional: create a dedicated project (CREATE_PROJECT=1) ---------------------------------
# Off by default. Needs ORGANIZATION_ID (or FOLDER_ID) and BILLING_ACCOUNT_ID, plus permission to
# create projects and link billing. The project is labelled so teardown (DELETE_PROJECT=1) will only
# ever delete a project this script created.
if [ "${CREATE_PROJECT:-0}" = "1" ]; then
  PARENT=""
  [ -n "${ORGANIZATION_ID:-}" ] && PARENT="--organization=${ORGANIZATION_ID}"
  [ -n "${FOLDER_ID:-}" ] && PARENT="--folder=${FOLDER_ID}"
  [ -n "${PARENT}" ] || { echo "ERROR: CREATE_PROJECT=1 needs ORGANIZATION_ID or FOLDER_ID" >&2; exit 1; }
  [ -n "${BILLING_ACCOUNT_ID:-}" ] || { echo "ERROR: CREATE_PROJECT=1 needs BILLING_ACCOUNT_ID" >&2; exit 1; }
  if [ -n "$(terraform -chdir="${TF_DIR}" state list 2>/dev/null)" ]; then
    echo "ERROR: ${TF_DIR} still tracks an earlier environment; run teardown first." >&2
    exit 1
  fi
  PROJECT_ID="${PROJECT_ID:-${ENV_NAME:0:20}-p-$(openssl rand -hex 3)}"
  echo "► Creating project ${PROJECT_ID} (${PARENT#--}) and linking billing..."
  gcloud projects create "${PROJECT_ID}" ${PARENT} --name="${ENV_NAME} protected" \
    --labels="created-by=${CREATED_BY_LABEL},env=${ENV_NAME}" --quiet
  gcloud billing projects link "${PROJECT_ID}" --billing-account="${BILLING_ACCOUNT_ID}" --quiet
  echo "${PROJECT_ID}" > "${STATE_FILE}"
  echo "  ✓ Project ${PROJECT_ID} created"
  # The creator's roles take a few minutes to propagate; Terraform would fail with USER_PROJECT_DENIED.
  echo "► Waiting for the new project to accept requests from this account..."
  READY=0
  for _ in $(seq 1 "${PROJECT_READY_TRIES:-40}"); do
    TOKEN="$(gcloud auth application-default print-access-token 2>/dev/null || true)"
    CODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" \
      -H "x-goog-user-project: ${PROJECT_ID}" "https://cloudresourcemanager.googleapis.com/v3/projects/${PROJECT_ID}" || true)"
    if [ "${CODE}" = "200" ]; then READY=1; break; fi
    sleep 15
  done
  [ "${READY}" = "1" ] || { echo "ERROR: ${PROJECT_ID} did not become usable in time; rerun provision.sh without CREATE_PROJECT to resume." >&2; exit 1; }
  echo "  ✓ Project ready"
  echo ""
fi
PROJECT_ID="${PROJECT_ID:-$(cat "${STATE_FILE}" 2>/dev/null || true)}"
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID (or CREATE_PROJECT=1 with ORGANIZATION_ID and BILLING_ACCOUNT_ID)}"

ACTIVE_ACCOUNT="$(gcloud config get-value account 2>/dev/null)"
SEARCH_MEMBERS="${SEARCH_MEMBERS:-}"          # users/groups allowed to search, e.g. user:a@example.com,group:team@example.com
BACKEND_INVOKERS="${BACKEND_INVOKERS:-}"      # trusted backend, e.g. serviceAccount:backend@project.iam.gserviceaccount.com
INGEST_OPERATORS="${INGEST_OPERATORS:-user:${ACTIVE_ACCOUNT}}"

echo ""
echo "=== Provision (protected mode): private corpus + ACL-enabled Search ==="
echo "  Project: ${PROJECT_ID}"
echo ""

echo "► Enabling required GCP APIs..."
gcloud services enable \
  cloudresourcemanager.googleapis.com run.googleapis.com aiplatform.googleapis.com \
  discoveryengine.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  storage.googleapis.com iam.googleapis.com iamcredentials.googleapis.com serviceusage.googleapis.com \
  --project="${PROJECT_ID}"
echo "  ✓ APIs enabled — waiting ${API_WAIT_SECONDS:-30}s for propagation..."
sleep "${API_WAIT_SECONDS:-30}"
echo ""

echo "► Provisioning protected infrastructure with Terraform..."
terraform -chdir="${TF_DIR}" init -upgrade -input=false -reconfigure
terraform -chdir="${TF_DIR}" apply -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}" \
  -var="manage_acl_config=${MANAGE_ACL_CONFIG:-true}" \
  -var="idp_type=${IDP_TYPE:-GSUITE}" \
  -var="workforce_pool_name=${WORKFORCE_POOL_NAME:-}" \
  -var="search_members=$(json_list "${SEARCH_MEMBERS}")" \
  -var="backend_invokers=$(json_list "${BACKEND_INVOKERS}")" \
  -var="ingest_operators=$(json_list "${INGEST_OPERATORS}")"
echo "  ✓ Infrastructure ready"
echo ""

BUCKET=$(terraform -chdir="${TF_DIR}" output -raw docs_bucket)
DATASTORE_ID=$(terraform -chdir="${TF_DIR}" output -raw datastore_id)
ENGINE_ID=$(terraform -chdir="${TF_DIR}" output -raw search_engine_id)

if [ -n "${PROTECTED_CORPUS_DIR:-}" ]; then
  echo "► Ingesting the approved corpus from ${PROTECTED_CORPUS_DIR} ..."
  VENV="${GCP_VENV:-/tmp/gcp-agent-venv}"
  [ -x "${VENV}/bin/python3" ] || python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install -q google-auth requests google-cloud-storage
  "${VENV}/bin/python3" "${SCRIPT_DIR}/ingest_protected.py" \
    --project "${PROJECT_ID}" --bucket "${BUCKET}" --datastore "${DATASTORE_ID}" \
    --engine "${ENGINE_ID}" --directory "${PROTECTED_CORPUS_DIR}"
  echo ""
fi

echo "=== Provision Complete (protected) ==="
echo "  Private bucket: gs://${BUCKET}"
echo "  Data store    : ${DATASTORE_ID} (ACL-enabled)"
echo "  Search engine : ${ENGINE_ID}"
echo ""
echo "  Next: bash scripts/deploy.sh   (KB_ACCESS_MODE=protected)"
echo "  Ingest corpus : python3 scripts/ingest_protected.py --project ${PROJECT_ID} --bucket ${BUCKET} \\"
echo "                    --datastore ${DATASTORE_ID} --engine ${ENGINE_ID} --directory /path/outside/repo"
echo ""
