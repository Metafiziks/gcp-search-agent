#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID env var}"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-central1}"
GITHUB_REPO="${GITHUB_REPO:?Set GITHUB_REPO (owner/repo)}"

echo ""
echo "=== Provision: Infrastructure + Search Setup ==="
echo ""

# --- Prerequisite: Vertex AI terms of service ---
# GCP requires a one-time console acceptance of Vertex AI Generative AI terms.
# This cannot be automated via CLI — it must be done before first use.
echo "┌─────────────────────────────────────────────────────────────────┐"
echo "│  PREREQUISITE: Vertex AI must be activated in the GCP Console   │"
echo "│                                                                  │"
echo "│  1. Open this URL in your browser:                               │"
echo "│     https://console.cloud.google.com/vertex-ai?project=${PROJECT_ID}"
echo "│                                                                  │"
echo "│  2. Click 'Enable APIs' if prompted, then return here.           │"
echo "│                                                                  │"
echo "│  This is a one-time step per project. Skip if already done.      │"
echo "└─────────────────────────────────────────────────────────────────┘"
echo ""
read -r -p "  Press Enter once Vertex AI is activated to continue..." _
echo ""

# --- Bootstrap: enable all required APIs via gcloud before Terraform runs ---
# CRM must be enabled first (chicken-and-egg with Terraform IAM resources).
# Enable all APIs upfront so Terraform doesn't race against propagation.
echo "► Bootstrapping required GCP APIs (this takes ~1 min)..."
gcloud services enable \
  cloudresourcemanager.googleapis.com \
  run.googleapis.com \
  aiplatform.googleapis.com \
  discoveryengine.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  generativelanguage.googleapis.com \
  --project="${PROJECT_ID}"
echo "  ✓ APIs enabled — waiting 30s for propagation..."
sleep 30
echo ""

# --- Terraform ---
echo "► Provisioning infrastructure with Terraform..."
terraform -chdir=terraform init -upgrade -input=false -reconfigure
terraform -chdir=terraform apply -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}" \
  -var="github_repo=${GITHUB_REPO}"
echo "  ✓ Infrastructure ready"
echo ""

BUCKET=$(terraform -chdir=terraform output -raw docs_bucket)
DATASTORE_ID=$(terraform -chdir=terraform output -raw datastore_id)
SEARCH_ENGINE_ID=$(terraform -chdir=terraform output -raw search_engine_id)

# --- Upload documents ---
echo "► Uploading documents from docs/ to GCS..."
gcloud storage rsync -r docs/ "gs://${BUCKET}/docs/" --project="${PROJECT_ID}"
echo "  ✓ Documents uploaded"
echo ""

# --- Import into Vertex AI Search ---
echo "► Importing documents into Vertex AI Search..."
pip install -q google-cloud-discoveryengine
python3 scripts/import_docs.py "$PROJECT_ID" "$DATASTORE_ID" "$BUCKET"
echo ""

# --- Generate eval cases from docs ---
echo "► Generating eval cases from docs/..."
pip install -q google-genai
PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" \
  python3 scripts/generate_eval_cases.py
echo ""

echo "=== Provision Complete ==="
echo "  Bucket       : gs://${BUCKET}"
echo "  Data store   : ${DATASTORE_ID}"
echo "  Search engine: ${SEARCH_ENGINE_ID}"
echo ""

# Persist values for deploy step
echo "SEARCH_DATASTORE_ID=${DATASTORE_ID}" >> "${GITHUB_ENV:-/dev/null}" 2>/dev/null || true
echo "DOCS_BUCKET=${BUCKET}" >> "${GITHUB_ENV:-/dev/null}" 2>/dev/null || true
