#!/usr/bin/env bash
# Protected mode deploy (KB_ACCESS_MODE=protected). Called from deploy.sh.
# Deploys a private, stateless Cloud Run service: no allUsers invoker, no API keys, no
# sample evals, no BigQuery telemetry. Only the trusted backend may call it.
set -euo pipefail

STATE_FILE="${PROTECTED_STATE_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.protected-project}"
PROJECT_ID="${PROJECT_ID:-$(cat "${STATE_FILE}" 2>/dev/null || true)}"
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID env var}"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/../terraform/protected"
SERVICE="${ENV_NAME}-protected"
BACKEND_INVOKERS="${BACKEND_INVOKERS:-}"

echo ""
echo "=== Deploy (protected mode): private Cloud Run service ==="
echo ""

BUCKET=$(terraform -chdir="${TF_DIR}" output -raw docs_bucket)
DATASTORE_ID=$(terraform -chdir="${TF_DIR}" output -raw datastore_id)
ENGINE_ID=$(terraform -chdir="${TF_DIR}" output -raw search_engine_id)
AGENT_SA=$(terraform -chdir="${TF_DIR}" output -raw agent_service_account)

echo "► Building and deploying ${SERVICE}..."
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT_ID}" --region "${REGION}" \
  --source "${SCRIPT_DIR}/../src/protected" \
  --service-account "${AGENT_SA}" \
  --no-allow-unauthenticated \
  --memory 512Mi --max-instances 3 \
  --set-env-vars "PROJECT_ID=${PROJECT_ID},SEARCH_DATASTORE_ID=${DATASTORE_ID},SEARCH_ENGINE_ID=${ENGINE_ID},SEARCH_LOCATION=global,DOCS_BUCKET=${BUCKET},GEMINI_LOCATION=${GEMINI_LOCATION:-global},GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash}" \
  --quiet
echo "  ✓ Service deployed"
echo ""

IFS=',' read -r -a INVOKERS <<< "${BACKEND_INVOKERS}"
for member in "${INVOKERS[@]}"; do
  member="$(echo "${member}" | xargs)"
  [ -z "${member}" ] && continue
  gcloud run services add-iam-policy-binding "${SERVICE}" \
    --project "${PROJECT_ID}" --region "${REGION}" \
    --member "${member}" --role roles/run.invoker --quiet >/dev/null
  echo "  ✓ ${member} may invoke ${SERVICE}"
done

# Refuse to leave the service reachable by the public.
POLICY="$(gcloud run services get-iam-policy "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --format=json)"
if echo "${POLICY}" | grep -Eq 'allUsers|allAuthenticatedUsers'; then
  echo "ERROR: ${SERVICE} has a public invoker binding; remove it before use." >&2
  exit 1
fi

SERVICE_URL=$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --format="value(status.url)")
echo "=== Deploy Complete (protected) ==="
echo "  Service URL : ${SERVICE_URL} (private; invoker IAM + ID token required)"
echo ""

if [ -n "${PROTECTED_ACCEPTANCE_CONFIG:-}" ]; then
  echo "► Running the protected acceptance gate (prompts for tokens; fails the deploy if it fails)..."
  VENV="${GCP_VENV:-/tmp/gcp-agent-venv}"
  [ -x "${VENV}/bin/python3" ] || python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install -q httpx google-auth requests google-cloud-storage
  "${VENV}/bin/python3" "${SCRIPT_DIR}/test_protected_live.py" \
    --service-url "${SERVICE_URL}" --bucket "${BUCKET}" --project "${PROJECT_ID}" \
    --datastore "${DATASTORE_ID}" --engine "${ENGINE_ID}" --config "${PROTECTED_ACCEPTANCE_CONFIG}" \
    ${PROTECTED_ACCEPTANCE_ARGS:-}   # e.g. --tokens-from-gcloud --backend-account <acct> for a non-interactive run
else
  echo "  No PROTECTED_ACCEPTANCE_CONFIG set: deployed without the live acceptance gate."
  echo "  Run scripts/test_protected_live.py (see guides/protected-mode.md) before accepting traffic."
fi
echo ""
