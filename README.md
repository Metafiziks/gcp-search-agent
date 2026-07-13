# gcp-search-agent

A Terraform + Google ADK template that deploys a **Gemini Enterprise Agent Platform + Cloud Run hosted agent** backed by your own document corpus. Ask questions in natural language — the agent synthesizes answers and cites source files with direct links.

```
$ SESSION=$(curl -s -X POST https://<your-service>/apps/agent/users/u1/sessions \
    -H 'Content-Type: application/json' -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

$ curl -s -X POST https://<your-service>/run \
    -H 'Content-Type: application/json' \
    -d "{\"appName\":\"agent\",\"userId\":\"u1\",\"sessionId\":\"$SESSION\", \
        \"newMessage\":{\"role\":\"user\",\"parts\":[{\"text\":\"What should I do if a hydraulic press is leaking oil?\"}]}}" \
  | python3 -c "import sys,json; events=json.load(sys.stdin); [print(p['text']) for e in events for p in e.get('content',{}).get('parts',[]) if 'text' in p]"

If you discover that a hydraulic press is leaking oil, you should immediately stop the machine
and notify maintenance personnel. Do not operate the press while there are active hydraulic leaks.
Use absorbent pads and follow established spill response procedures.

[hydraulic_press_troubleshooting](https://storage.googleapis.com/<bucket>/docs/maintenance/hydraulic_press_troubleshooting.txt)
```

## What it deploys

| Resource | Purpose |
|---|---|
| Cloud Run | Hosts the ADK agent |
| Vertex AI Search (Enterprise) | RAG — indexes and retrieves documents with extractive answers |
| Cloud Storage | Stores the document corpus (public read for citation links) |
| Artifact Registry | Container images for agent builds |
| Workload Identity Federation | Secretless GitHub Actions auth |

## Architecture

```
User question
     │
     ▼
Cloud Run (Google ADK — Gemini 2.5 Flash via Agent Platform)
     │  calls search_knowledge_base()
     ▼
Vertex AI Search Enterprise (LLM add-on, extractive answers)
     │  indexed from
     ▼
Cloud Storage (docs/ bucket, public read, HTTPS citation links)
```

## Prerequisites

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud`)
- [Terraform](https://developer.hashicorp.com/terraform/install) ≥ 1.5 — `brew install hashicorp/tap/terraform`
- [Google ADK](https://google.github.io/adk-docs/) — `pipx install google-adk`
- GCP project with billing enabled
- Owner or sufficient IAM on the project

> **One-time console step:** Before running `provision.sh`, visit  
> `https://console.cloud.google.com/vertex-ai?project=YOUR_PROJECT_ID`  
> and click **Enable APIs** to activate the Agent Platform bundle and accept terms.  
> The script will pause and prompt you for this.

## Quick start

```bash
git clone https://github.com/Metafiziks/gcp-search-agent
cd gcp-search-agent

# Auth
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID

# Set required env vars
export PROJECT_ID=your-gcp-project-id
export GITHUB_REPO=your-org/your-repo   # for Workload Identity setup
export ENV_NAME=search-agent            # drives all resource names
export REGION=us-central1

# Step 1: Provision infrastructure + index documents (~5-10 min)
bash scripts/provision.sh

# Step 2: Deploy agent to Cloud Run (~3 min)
bash scripts/deploy.sh
```

## Adding your documents

Drop `.txt` files into `docs/` before running `provision.sh`:

```
docs/
  safety/       lockout_tagout.txt
  maintenance/  hydraulic_press_guide.txt
  quality/      inspection_standard.txt
```

Any folder structure is preserved as the GCS path and appears in citation links.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `PROJECT_ID` | ✅ | — | GCP project ID |
| `GITHUB_REPO` | ✅ | — | `owner/repo` for Workload Identity |
| `ENV_NAME` | — | `search-agent` | Prefix for all resource names |
| `REGION` | — | `us-central1` | Cloud Run + Artifact Registry region |
| `GEMINI_MODEL` | — | `gemini-2.5-flash` | Override the Gemini model |

## GitHub Actions CI/CD

After provisioning, set these repo variables:

```bash
WIF_PROVIDER=$(terraform -chdir=terraform output -raw wif_provider)
WIF_SERVICE_ACCOUNT=$(terraform -chdir=terraform output -raw deployer_service_account)

gh variable set WIF_PROVIDER --body "$WIF_PROVIDER"
gh variable set WIF_SERVICE_ACCOUNT --body "$WIF_SERVICE_ACCOUNT"
gh variable set GCP_PROJECT_ID --body "$PROJECT_ID"
```

Then every push to `main` automatically redeploys the agent via `.github/workflows/deploy.yml`.

## Comparison with Azure equivalent

| | This template (GCP) | [azd-foundry-search-agent](https://github.com/Metafiziks/azd-foundry-search-agent) (Azure) |
|---|---|---|
| Provision | `terraform apply` | `azd provision` (Bicep) |
| Deploy | `adk deploy cloud_run` | `azd deploy` (remote_build) |
| LLM | Gemini 2.5 Flash (Agent Platform) | GPT-4o (Azure AI Foundry) |
| Search | Vertex AI Search Enterprise | Azure AI Search |
| Auth | Workload Identity Federation | Azure OIDC |
| CI/CD | GitHub Actions | GitHub Actions |

## Troubleshooting

| Error | Fix |
|---|---|
| `cloudresourcemanager.googleapis.com` not enabled | Run `gcloud services enable cloudresourcemanager.googleapis.com` first |
| `Publisher model ... was not found` | Enable Agent Platform APIs in the console (see prerequisite above) |
| `Cannot use enterprise edition features` | Upgrade search engine tier via REST PATCH or recreate with `SEARCH_TIER_ENTERPRISE` |
| `serving config not found` | Ensure serving config name is `default_search` (not `default_config`) |
| `gsutil` Python version error | Use `gcloud storage rsync` instead |
