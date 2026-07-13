#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID env var}"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"

echo ""
echo "=== Deploy: Agent to Cloud Run ==="
echo ""

DATASTORE_ID="${SEARCH_DATASTORE_ID:-$(terraform -chdir=terraform output -raw datastore_id)}"
AGENT_SA=$(terraform -chdir=terraform output -raw agent_service_account)

echo "► Deploying agent with ADK..."
adk deploy cloud_run \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --service-name "search-agent" \
  --service-account "$AGENT_SA" \
  --set-env-vars "PROJECT_ID=${PROJECT_ID},SEARCH_DATASTORE_ID=${DATASTORE_ID},SEARCH_LOCATION=global" \
  ./src/agent
echo "  ✓ Agent deployed"
echo ""

SERVICE_URL=$(gcloud run services describe search-agent \
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
