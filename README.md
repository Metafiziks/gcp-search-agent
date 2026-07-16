# gcp-search-agent

A Terraform + Google ADK template that deploys a **Gemini Enterprise Agent Platform + Cloud Run hosted agent** backed by your own document corpus. Ask questions in natural language — the agent synthesizes answers and cites source files with direct links. Includes a built-in **automated evaluation suite** that scores every deployment on faithfulness, answer relevance, citation accuracy, and latency using Gemini 2.5 Flash as an LLM judge.

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
| Vertex AI Search (Enterprise) | RAG — indexes documents and retrieves relevant passages (extractive answers) for the LLM to synthesize |
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

# Tear everything down when done
bash scripts/teardown.sh
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

`deploy.sh` sets all required repo variables automatically if `gh` is authenticated. To set them manually:

```bash
gh variable set WIF_PROVIDER        --body "$(terraform -chdir=terraform output -raw wif_provider)"
gh variable set WIF_SERVICE_ACCOUNT --body "$(terraform -chdir=terraform output -raw deployer_service_account)"
gh variable set PROJECT_ID          --body "$PROJECT_ID"
gh variable set SERVICE_URL         --body "$(gcloud run services describe search-agent --region us-central1 --project $PROJECT_ID --format='value(status.url)')"
```

Then activate the workflows by copying them into `.github/workflows/`:

```bash
cp workflows/*.yml .github/workflows/
git add .github/workflows/ && git commit -m "Activate CI workflows" && git push
```

| Workflow | Trigger | What it does |
|---|---|---|
| `deploy.yml` | Push to `src/` | Rebuilds and redeploys the Cloud Run agent |
| `run-evals.yml` | Push to `src/` or `tests/`, weekly | Runs the 12-case eval suite via Gemini judge; fails CI if metrics drop below threshold |

No GCP credentials stored as secrets — all auth via Workload Identity Federation.

## Running Evaluations Locally

After deploying:

```bash
bash scripts/eval.sh
```

`eval.sh` resolves the Cloud Run service URL from `gcloud` automatically. To skip the Gemini judge:

```bash
bash scripts/eval.sh --no-judge
```

Results are written to `eval_results.json`. Metrics scored:

| Metric | Method | Pass threshold |
|---|---|---|
| Keyword Recall | Deterministic | ≥ 0.65 |
| Citation Recall | Deterministic | ≥ 0.60 |
| p95 Latency | Deterministic | ≤ 25000ms |
| Faithfulness | Gemini 2.5 Flash judge | ≥ 0.70 |
| Answer Relevance | Gemini 2.5 Flash judge | ≥ 0.75 |

**Keeping evals in sync with your docs:**

When you change files in `docs/`, regenerate the eval cases before re-running:

```bash
PROJECT_ID=your-project-id python3 scripts/generate_eval_cases.py
bash scripts/eval.sh
```

`generate_eval_cases.py` reads every `.txt` file under `docs/`, calls Gemini 2.5 Flash to generate 2 Q&A test cases per document, and writes `tests/eval_cases.json`. Running `bash scripts/provision.sh` does this automatically after each doc sync.

## Comparison across cloud providers

| | This template (GCP) | [AWS](https://github.com/Metafiziks/aws-bedrock-agent) | [Azure](https://github.com/Metafiziks/azd-foundry-search-agent) |
|---|---|---|---|
| Provision | `bash scripts/provision.sh` | `bash scripts/provision.sh` | `azd provision` |
| LLM | Gemini 2.5 Flash (Vertex AI) | Amazon Nova Lite (Bedrock) | GPT-5 (Azure AI Foundry) |
| Agent SDK | Google ADK + Cloud Run | Bedrock Agents (managed) | AI Foundry hosted agent |
| RAG | Vertex AI Search Enterprise | Bedrock Knowledge Bases | Azure AI Search |
| Vector store | Vertex AI Search (built-in) | OpenSearch Serverless | Azure AI Search (built-in) |
| Auth | Workload Identity Federation | GitHub OIDC | Azure OIDC |
| Eval judge | Gemini 2.5 Flash | Amazon Nova Pro | GPT-5 |
| Teardown | `bash scripts/teardown.sh` | `bash scripts/teardown.sh` | `azd down` |

## Teardown

```bash
export PROJECT_ID=your-gcp-project-id
export ENV_NAME=search-agent   # must match what you used for provision
export REGION=us-central1

bash scripts/teardown.sh
```

Deletes the Cloud Run service first (deployed outside Terraform by ADK), then runs `terraform destroy` to remove all remaining resources — storage bucket, Vertex AI Search engine, data store, service accounts, Workload Identity pool, and Artifact Registry.

## Troubleshooting

| Error | Fix |
|---|---|
| `cloudresourcemanager.googleapis.com` not enabled | Run `gcloud services enable cloudresourcemanager.googleapis.com` first |
| `Publisher model ... was not found` | Enable Agent Platform APIs in the console (see prerequisite above) |
| `Cannot use enterprise edition features` | Upgrade search engine tier via REST PATCH or recreate with `SEARCH_TIER_ENTERPRISE` |
| `serving config not found` | Ensure serving config name is `default_search` (not `default_config`) |
| `gsutil` Python version error | Use `gcloud storage rsync` instead |
