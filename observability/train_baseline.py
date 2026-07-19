#!/usr/bin/env python3
"""
Bootstrap IsolationForest training from eval telemetry.

Reads baseline rows from BigQuery (rows written with is_baseline=True during
the deploy-time eval run), then falls back to recent runtime retrieval rows
created by the deploy-time eval if explicit baseline rows are unavailable.
It builds the feature matrix, trains an IForest model, and uploads it to GCS
for the agent to load at startup.

Usage:
    PROJECT_ID=my-project \
    GCS_MODEL_PATH=gs://my-bucket/models/iforest.pkl \
    python3 observability/train_baseline.py

Requires: pip install -r observability/requirements.txt
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from observability.isolation_forest import train_and_upload
from observability.shared.features import from_bq_row

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ID     = os.environ["PROJECT_ID"]
DATASET_ID     = os.environ.get("BQ_DATASET_ID", "agent_observability")
GCS_MODEL_PATH = os.environ["GCS_MODEL_PATH"]
LOCAL_MODEL    = "/tmp/iforest_baseline.pkl"
MIN_ROWS       = int(os.environ.get("MIN_BASELINE_ROWS", "20"))
LOOKBACK_MINUTES = int(os.environ.get("BASELINE_LOOKBACK_MINUTES", "120"))


def load_baseline_rows() -> list[dict]:
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT_ID)
    explicit_query = f"""
        SELECT *
        FROM `{PROJECT_ID}.{DATASET_ID}.telemetry`
        WHERE is_baseline = TRUE
          AND retrieval_score_mean IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1000
    """
    rows = [dict(r) for r in client.query(explicit_query).result()]
    if len(rows) >= MIN_ROWS:
        return rows

    logger.info(
        "Found %d explicit baseline retrieval rows; falling back to runtime rows from the last %d minutes.",
        len(rows),
        LOOKBACK_MINUTES,
    )
    fallback_query = f"""
        SELECT *
        FROM `{PROJECT_ID}.{DATASET_ID}.telemetry`
        WHERE source = 'runtime'
          AND retrieval_score_mean IS NOT NULL
          AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {LOOKBACK_MINUTES} MINUTE)
        ORDER BY timestamp DESC
        LIMIT 1000
    """
    return [dict(r) for r in client.query(fallback_query).result()]


def main() -> None:
    logger.info("Loading baseline telemetry from BigQuery...")
    rows = load_baseline_rows()
    logger.info("Found %d baseline rows", len(rows))

    if len(rows) < MIN_ROWS:
        logger.error(
            "Only %d rows found (need >= %d). "
            "Run evals first to populate baseline data.",
            len(rows), MIN_ROWS,
        )
        sys.exit(1)

    logger.info("Building feature matrix...")
    X = np.array([from_bq_row(r) for r in rows])
    logger.info("Feature matrix: %s", X.shape)

    logger.info("Training IsolationForest...")
    model = train_and_upload(
        X=X,
        gcs_path=GCS_MODEL_PATH,
        project_id=PROJECT_ID,
        local_path=LOCAL_MODEL,
    )
    logger.info(
        "IForest trained: %d estimators, contamination=%.2f",
        model.n_estimators, model.contamination,
    )
    print(f"\n✓ IsolationForest trained on {len(X)} baseline samples")
    print(f"✓ Model uploaded → {GCS_MODEL_PATH}")
    print(f"  Features: {X.shape[1]}-dimensional")
    print(f"  The agent will load this model at startup via GCS_MODEL_PATH env var")


if __name__ == "__main__":
    main()
