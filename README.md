# gcp-search-agent

A Terraform + Google ADK template that deploys a **Vertex AI + Cloud Run hosted agent** backed by your own document corpus. Ask questions in natural language — the agent synthesizes answers and cites source files with direct links.

```
Agent: What should a floor supervisor do when equipment fails?

[search-agent] Here's a practical sequence broken down by safety, quality,
and maintenance...

Sources:
- [Lockout Tagout Procedure](https://storage.googleapis.com/project-search-agent-docs/docs/safety/lockout_tagout_procedure.txt)
- [Hydraulic Press Troubleshooting Guide](https://storage.googleapis.com/project-search-agent-docs/docs/maintenance/hydraulic_press_troubleshooting.txt)
```

## What it deploys

| Resource | Purpose |
|---|---|
| Cloud Run | Hosts the agent (via Google ADK) |
| Vertex AI Search | Indexes and retrieves documents with extractive answers |
| Cloud Storage | Stores the document corpus (public read for citation links) |
| Artifact Registry | Stores agent container images |
| Workload Identity | Secretless GitHub Actions auth |

## Architecture

```
User question
     │
     ▼
Cloud Run (Google ADK agent — Gemini 2.0 Flash)
     │  calls search_knowledge_base()
     ▼
Vertex AI Search (LLM add-on, extractive answers)
     │  indexed from
     ▼
Cloud Storage  (docs/ bucket, public read)
```

## Prerequisites

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud`)
- [Terraform](https://developer.hashicorp.com/terraform/install) `>= 1.5`
- [Google ADK](https://google.github.io/adk-docs/) (`pip install google-adk`)
- GCP project with billing enabled
- Owner or sufficient IAM on the project (to create service accounts and enable APIs)

```bash
pip install google-adk google-cloud-discoveryengine
```

## Quick start

```bash
git clone https://github.com/Metafiziks/gcp-search-agent
cd gcp-search-agent

gcloud auth login
gcloud auth application-default login

export PROJECT_ID=your-gcp-project-id
export GITHUB_REPO=your-org/gcp-search-agent   # for Workload Identity setup

bash scripts/provision.sh   # terraform apply + upload docs + index
bash scripts/deploy.sh       # adk deploy cloud_run
```

The full pipeline runs in ~10 minutes on a fresh project:

| Step | What happens |
|---|---|
| `provision.sh` | Enables APIs, creates GCS/Vertex AI Search/Artifact Registry via Terraform |
| | Uploads `docs/` to GCS |
| | Imports documents into Vertex AI Search and waits for indexing |
| `deploy.sh` | Builds agent container, pushes to Artifact Registry, deploys to Cloud Run |

## GitHub Actions (CI/CD)

The workflow uses **Workload Identity Federation** — no stored secrets.

**One-time setup** (run after first `provision.sh`):

```bash
# Get outputs from Terraform
WIF_PROVIDER=$(terraform -chdir=terraform output -raw wif_provider)
WIF_SA=$(terraform -chdir=terraform output -raw deployer_service_account)

# Set as GitHub repo variables
gh variable set WIF_PROVIDER --body "$WIF_PROVIDER"
gh variable set WIF_SERVICE_ACCOUNT --body "$WIF_SA"
gh variable set GCP_PROJECT_ID --body "$PROJECT_ID"
```

Then every push to `main` runs `provision.sh` + `deploy.sh` automatically.

## Bring your own documents

1. Replace files in `docs/` with your own content
2. Supported formats: `.txt`, `.pdf`, `.docx`, `.md`
3. Subdirectory structure becomes document metadata (e.g., `docs/hr/`, `docs/legal/`)
4. Re-run `bash scripts/provision.sh && bash scripts/deploy.sh`

Optionally update the agent system prompt in `src/agent/agent.py`:

```python
INSTRUCTIONS = """
You are a knowledgeable assistant that answers questions based on
[your org]'s documents...
"""
```

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `PROJECT_ID` | GCP project ID | required |
| `ENV_NAME` | Environment name — drives all resource names | `search-agent` |
| `REGION` | GCP region for Cloud Run + Artifact Registry | `us-central1` |
| `GITHUB_REPO` | `owner/repo` for Workload Identity Federation | required |

## Cleanup

```bash
terraform -chdir=terraform destroy \
  -var="project_id=${PROJECT_ID}" \
  -var="github_repo=${GITHUB_REPO}"

# Also delete Cloud Run service (not managed by Terraform — deployed by ADK)
gcloud run services delete search-agent --region us-central1 --project $PROJECT_ID
```

## Comparison with Azure equivalent

| | Azure (`azd-foundry-search-agent`) | GCP (`gcp-search-agent`) |
|---|---|---|
| LLM | Azure OpenAI gpt-5 | Vertex AI Gemini 2.0 Flash |
| Search | Azure AI Search | Vertex AI Search |
| Storage | Azure Blob Storage | Cloud Storage |
| Agent hosting | Azure AI Foundry (managed) | Cloud Run via Google ADK |
| Container build | Foundry `remote_build` (automatic) | ADK + Cloud Build (explicit) |
| IaC | `azd` / Bicep | Terraform |
| Auth | Azure OIDC | Workload Identity Federation |
| One-command deploy | `azd up` | `bash scripts/provision.sh && bash scripts/deploy.sh` |

## Troubleshooting

**`gcloud: command not found`**
Install the [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) and run `gcloud auth login`.

**Terraform `Permission denied` on API enable**
Your account needs `roles/serviceusage.serviceUsageAdmin` on the project.

**Vertex AI Search import takes too long**
Document import is asynchronous. The script polls for up to 10 minutes; large corpora may take longer. Check progress in [Cloud Console → Vertex AI Search](https://console.cloud.google.com/discovery).

**ADK deploy fails with auth error**
Run `gcloud auth application-default login` to refresh Application Default Credentials.

**Citation links return 403**
Run `bash scripts/provision.sh` again — Terraform will set the bucket to public read.
