# BigQuery dataset + telemetry table for ML observability

resource "google_project_service" "bigquery" {
  service            = "bigquery.googleapis.com"
  disable_on_destroy = false
  depends_on         = [google_project_service.apis]
}

resource "google_bigquery_dataset" "observability" {
  dataset_id    = "agent_observability"
  friendly_name = "Agent Observability"
  description   = "RAG agent telemetry for drift detection and hallucination monitoring"
  location      = "US"

  depends_on = [google_project_service.bigquery]
}

resource "google_bigquery_table" "telemetry" {
  dataset_id          = google_bigquery_dataset.observability.dataset_id
  table_id            = "telemetry"
  deletion_protection = false

  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }

  schema = jsonencode([
    { name = "timestamp", type = "TIMESTAMP", mode = "REQUIRED",
    description = "UTC time of the request" },
    { name = "request_id", type = "STRING", mode = "REQUIRED",
    description = "UUID identifying this search call" },
    { name = "query", type = "STRING", mode = "NULLABLE",
    description = "User query (truncated to 1024 chars)" },
    { name = "source", type = "STRING", mode = "NULLABLE",
    description = "Origin: runtime | eval | memory" },

    # ── Retrieval metrics (populated by agent at runtime) ──────────────────
    { name = "search_latency_ms", type = "FLOAT64", mode = "NULLABLE",
    description = "Wall-clock time for Vertex AI Search call (ms)" },
    { name = "retrieval_score_mean", type = "FLOAT64", mode = "NULLABLE",
    description = "Mean relevance score across retrieved chunks" },
    { name = "retrieval_score_std", type = "FLOAT64", mode = "NULLABLE",
    description = "Std-dev of chunk relevance scores" },
    { name = "retrieval_score_entropy", type = "FLOAT64", mode = "NULLABLE",
    description = "Shannon entropy of score distribution" },
    { name = "chunk_count", type = "INT64", mode = "NULLABLE",
    description = "Number of chunks returned by search" },
    { name = "reranker_score_mean", type = "FLOAT64", mode = "NULLABLE",
    description = "Mean score from Vertex AI Ranking API" },

    # ── Anomaly detection (populated by agent at runtime) ─────────────────
    { name = "anomaly_score", type = "FLOAT64", mode = "NULLABLE",
    description = "IsolationForest decision_function value (negative = anomalous)" },
    { name = "is_anomaly", type = "BOOL", mode = "NULLABLE",
    description = "True when IForest predicts this request is an outlier" },
    { name = "is_baseline", type = "BOOL", mode = "NULLABLE",
    description = "True for rows written during deploy-time baseline collection" },

    # ── Answer quality metrics (populated by eval runner, null at runtime) ─
    { name = "answer_length", type = "INT64", mode = "NULLABLE",
    description = "Character count of the final answer" },
    { name = "citation_count", type = "INT64", mode = "NULLABLE",
    description = "Number of source citations in the answer" },
    { name = "hhem_score", type = "FLOAT64", mode = "NULLABLE",
    description = "HHEM hallucination probability [0=consistent, 1=hallucinated]" },
    { name = "latency_ms", type = "FLOAT64", mode = "NULLABLE",
    description = "End-to-end request latency (ms), from eval runner" },

    # ── Memory metrics (populated by memory tools and eval runner) ─────────
    { name = "memory_enabled", type = "BOOL", mode = "NULLABLE",
    description = "Whether the memory layer was enabled for this row" },
    { name = "memory_backend", type = "STRING", mode = "NULLABLE",
    description = "Memory backend: disabled | session_state | adk_memory | memory_bank" },
    { name = "memory_read_count", type = "INT64", mode = "NULLABLE",
    description = "Number of memory records read for the request/tool call" },
    { name = "memory_write_count", type = "INT64", mode = "NULLABLE",
    description = "Number of memory records written for the request/tool call" },
    { name = "memory_latency_ms", type = "FLOAT64", mode = "NULLABLE",
    description = "Memory read/write wall-clock time (ms)" },
  ])
}
