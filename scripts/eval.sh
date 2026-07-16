#!/usr/bin/env bash
# Run evaluations against the deployed GCP agent.
# Usage: bash scripts/eval.sh [--no-judge] [--output path/to/results.json]
#
# Reads SERVICE_URL from gcloud if not already set.
# PROJECT_ID must be set or already configured via gcloud.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: PROJECT_ID is not set and could not be resolved from gcloud config."
  echo "  Run: export PROJECT_ID=<your-gcp-project-id>"
  exit 1
fi

# Resolve SERVICE_URL from gcloud if not provided
if [[ -z "${SERVICE_URL:-}" ]]; then
  echo "► Resolving Cloud Run service URL..."
  SERVICE_URL=$(gcloud run services describe "${ENV_NAME}" \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --format="value(status.url)" 2>/dev/null || true)
  if [[ -z "${SERVICE_URL}" ]]; then
    echo "ERROR: Could not resolve SERVICE_URL. Is the agent deployed?"
    echo "  Run: bash scripts/provision.sh && bash scripts/deploy.sh"
    exit 1
  fi
  export SERVICE_URL
fi

# Ensure venv and dependencies
VENV_DIR="/tmp/gcp-eval-venv"
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "► Creating Python venv..."
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install requests google-genai -q

echo "► Running evaluations against: ${SERVICE_URL}"
echo ""

# Warm up the Cloud Run service before timed evaluation.
# Cloud Run cold starts can add 20-30s to the first request — this prevents
# that latency from inflating the p95 score.
echo "► Warming up agent (2 requests to prime the container)..."
for _ in 1 2; do
  SESSION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
  curl -sf -X POST "${SERVICE_URL}/apps/agent/users/warmup/sessions" \
    -H 'Content-Type: application/json' -d '{}' > /dev/null 2>&1 || true
  curl -sf -X POST "${SERVICE_URL}/run" \
    -H 'Content-Type: application/json' \
    -d "{\"appName\":\"agent\",\"userId\":\"warmup\",\"sessionId\":\"${SESSION_ID}\",\"newMessage\":{\"role\":\"user\",\"parts\":[{\"text\":\"ping\"}]}}" \
    > /dev/null 2>&1 || true
done
echo "  ✓ Warm-up complete"
echo ""

SERVICE_URL="${SERVICE_URL}" \
PROJECT_ID="${PROJECT_ID}" \
REGION="${REGION}" \
"${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/run_evals.py" \
  --output "${REPO_ROOT}/eval_results.json" \
  "$@"
