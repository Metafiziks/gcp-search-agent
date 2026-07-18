"""
BigQuery telemetry sink — fires telemetry rows in a background daemon thread
so the agent response path is never blocked by observability I/O.
"""
from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "")
DATASET_ID = os.environ.get("BQ_DATASET_ID", "agent_observability")
TABLE_ID   = os.environ.get("BQ_TABLE_ID",   "telemetry")

# Lazy BigQuery client — created once per process
_bq_client = None
_bq_lock   = threading.Lock()


def _client():
    global _bq_client
    if _bq_client is None:
        with _bq_lock:
            if _bq_client is None:
                from google.cloud import bigquery
                _bq_client = bigquery.Client(project=PROJECT_ID)
    return _bq_client


def _write(row: dict) -> None:
    """Synchronous write — called from a daemon thread."""
    try:
        table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
        errors = _client().insert_rows_json(table_ref, [row])
        if errors:
            logger.warning("BQ insert errors: %s", errors)
    except Exception as exc:
        logger.warning("Telemetry write failed (non-fatal): %s", exc)


def log_async(
    *,
    request_id: str,
    query: str,
    source: str = "runtime",
    search_latency_ms: float,
    retrieval_score_mean: float,
    retrieval_score_std: float,
    retrieval_score_entropy: float,
    chunk_count: int,
    reranker_score_mean: float,
    anomaly_score: float,
    is_anomaly: bool,
    is_baseline: bool = False,
    # answer metrics — populated by eval runner, null at runtime
    answer_length: Optional[int] = None,
    citation_count: Optional[int] = None,
    hhem_score: Optional[float] = None,
    latency_ms: Optional[float] = None,
) -> None:
    """
    Enqueue a telemetry row into BigQuery.
    Returns immediately; the write happens in a daemon thread.
    """
    row = {
        "timestamp":               datetime.datetime.utcnow().isoformat() + "Z",
        "request_id":              request_id,
        "query":                   query[:1024],  # cap at 1KB
        "source":                  source,
        "search_latency_ms":       round(search_latency_ms, 2),
        "retrieval_score_mean":    round(retrieval_score_mean, 6),
        "retrieval_score_std":     round(retrieval_score_std, 6),
        "retrieval_score_entropy": round(retrieval_score_entropy, 6),
        "chunk_count":             chunk_count,
        "reranker_score_mean":     round(reranker_score_mean, 6),
        "anomaly_score":           round(anomaly_score, 6),
        "is_anomaly":              is_anomaly,
        "is_baseline":             is_baseline,
        "answer_length":           answer_length,
        "citation_count":          citation_count,
        "hhem_score":              round(hhem_score, 6) if hhem_score is not None else None,
        "latency_ms":              round(latency_ms, 2) if latency_ms is not None else None,
    }
    threading.Thread(target=_write, args=(row,), daemon=True).start()
