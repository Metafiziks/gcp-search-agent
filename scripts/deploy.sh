#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID env var}"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"

echo ""
echo "=== Deploy: Agent to Cloud Run ==="
echo ""

DATASTORE_ID="${SEARCH_DATASTORE_ID:-$(terraform -chdir=terraform output -raw datastore_id)}"
ENGINE_ID="${SEARCH_ENGINE_ID:-$(terraform -chdir=terraform output -raw search_engine_id)}"
AGENT_SA=$(terraform -chdir=terraform output -raw agent_service_account)
BQ_DATASET_ID=$(terraform -chdir=terraform output -raw bq_dataset_id 2>/dev/null || echo "agent_observability")
GCS_MODEL_PATH=$(terraform -chdir=terraform output -raw gcs_model_path 2>/dev/null || echo "")

echo "► Deploying agent with ADK..."
adk deploy cloud_run \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --service_name "${ENV_NAME}" \
  ./src/agent \
  -- \
  --service-account "$AGENT_SA" \
  --memory 1Gi \
  --set-env-vars "PROJECT_ID=${PROJECT_ID},SEARCH_DATASTORE_ID=${DATASTORE_ID},SEARCH_ENGINE_ID=${ENGINE_ID},SEARCH_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=1,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},BQ_DATASET_ID=${BQ_DATASET_ID},GCS_MODEL_PATH=${GCS_MODEL_PATH}" \
  --allow-unauthenticated
echo "  ✓ Agent deployed"
echo ""

SERVICE_URL=$(gcloud run services describe "${ENV_NAME}" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format="value(status.url)")

echo "=== Deploy Complete ==="
echo "  Agent URL : ${SERVICE_URL}"
echo ""
echo "► Test your agent:"
echo "  curl -X POST ${SERVICE_URL}/run \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\": \"What documents are available?\"}'"
echo ""

# Set GitHub Actions repo variables
if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
  echo "► Setting GitHub Actions repo variables..."
  WIF_PROVIDER=$(terraform -chdir=terraform output -raw wif_provider)
  WIF_SA=$(terraform -chdir=terraform output -raw deployer_service_account)
  gh variable set WIF_PROVIDER        --body "${WIF_PROVIDER}"
  gh variable set WIF_SERVICE_ACCOUNT --body "${WIF_SA}"
  gh variable set PROJECT_ID          --body "${PROJECT_ID}"
  gh variable set SERVICE_URL         --body "${SERVICE_URL}"
  echo "  ✓ Repo variables set"
  echo ""
  echo "► To activate GitHub Actions workflows, copy them to .github/workflows/:"
  echo "  cp workflows/*.yml .github/workflows/"
  echo "  git add .github/workflows/ && git commit -m 'Activate CI workflows' && git push"
else
  echo "► Skipping GitHub Actions variable setup (gh CLI not authenticated)"
  echo "  Run manually after deploy:"
  echo "    gh variable set SERVICE_URL --body '${SERVICE_URL}'"
  echo "    gh variable set PROJECT_ID  --body '${PROJECT_ID}'"
fi
echo ""

echo "► Running automated evaluations (baseline collection for IsolationForest)..."
echo ""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="/tmp/gcp-eval-venv"

if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi
# Install eval deps + observability deps
"${VENV_DIR}/bin/pip" install requests google-genai google-cloud-bigquery -q
# Install HHEM deps if transformers available (may be large — skip with NO_HHEM=1)
if [[ "${NO_HHEM:-0}" != "1" ]]; then
  "${VENV_DIR}/bin/pip" install transformers torch --quiet 2>/dev/null || true
fi

# Run evals 5x to build a robust baseline corpus for the IsolationForest
BASELINE_RUNS="${BASELINE_RUNS:-5}"
for run in $(seq 1 "${BASELINE_RUNS}"); do
  echo "  [Baseline run ${run}/${BASELINE_RUNS}]"
  SERVICE_URL="${SERVICE_URL}" \
  PROJECT_ID="${PROJECT_ID}" \
  REGION="${REGION}" \
  BQ_DATASET_ID="${BQ_DATASET_ID}" \
  EVAL_IS_BASELINE="true" \
  "${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/run_evals.py" \
    --output "${SCRIPT_DIR}/../eval_results.json"
  echo ""
done

# Train IsolationForest on the collected baseline data
if [[ -n "${GCS_MODEL_PATH}" ]]; then
  echo "► Training IsolationForest on baseline telemetry..."
  "${VENV_DIR}/bin/pip" install scikit-learn numpy google-cloud-storage -q
  PROJECT_ID="${PROJECT_ID}" \
  GCS_MODEL_PATH="${GCS_MODEL_PATH}" \
  BQ_DATASET_ID="${BQ_DATASET_ID}" \
  "${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/../observability/train_baseline.py"
  echo ""
else
  echo "► Skipping IForest training (GCS_MODEL_PATH not set)"
fi
echo ""
