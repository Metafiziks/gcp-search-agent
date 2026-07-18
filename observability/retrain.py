#!/usr/bin/env python3
"""
Scheduled IsolationForest retraining — runs weekly via GitHub Actions.

Pulls the last 30 days of telemetry from BigQuery, retrains the IForest on
the accumulated production distribution, and replaces the GCS model artifact.

Over time the model shifts from "what healthy evals look like" to
"what healthy production traffic looks like" — a much better baseline.

Usage:
    PROJECT_ID=my-project \
    GCS_MODEL_PATH=gs://my-bucket/models/iforest.pkl \
    python3 observability/retrain.py

Requires: pip install -r observability/requirements.txt
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from observability.isolation_forest import train_and_upload, load_from_gcs
from observability.shared.features import from_bq_row

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ID     = os.environ["PROJECT_ID"]
DATASET_ID     = os.environ.get("BQ_DATASET_ID", "agent_observability")
GCS_MODEL_PATH = os.environ["GCS_MODEL_PATH"]
LOCAL_MODEL    = "/tmp/iforest_retrained.pkl"
LOOKBACK_DAYS  = int(os.environ.get("RETRAIN_LOOKBACK_DAYS", "30"))
MIN_ROWS       = int(os.environ.get("MIN_RETRAIN_ROWS", "20"))


def load_recent_rows() -> list[dict]:
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT_ID)
    query = f"""
        SELECT *
        FROM `{PROJECT_ID}.{DATASET_ID}.telemetry`
        WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {LOOKBACK_DAYS} DAY)
          AND retrieval_score_mean IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 5000
    """
    return [dict(r) for r in client.query(query).result()]


def load_all_baseline_rows() -> list[dict]:
    """Fallback: load all baseline rows when recent data is sparse."""
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT_ID)
    query = f"""
        SELECT *
        FROM `{PROJECT_ID}.{DATASET_ID}.telemetry`
        WHERE is_baseline = TRUE
          AND retrieval_score_mean IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1000
    """
    return [dict(r) for r in client.query(query).result()]


def main() -> None:
    logger.info("Fetching last %d days of telemetry...", LOOKBACK_DAYS)
    rows = load_recent_rows()
    logger.info("Found %d rows in lookback window", len(rows))

    if len(rows) < MIN_ROWS:
        logger.warning(
            "Only %d rows in last %d days — falling back to all baseline data",
            len(rows), LOOKBACK_DAYS,
        )
        rows = load_all_baseline_rows()
        logger.info("Baseline fallback: %d rows", len(rows))

    if len(rows) < 10:
        # Keep existing model rather than training on insufficient data
        logger.warning(
            "Insufficient data (%d rows). Keeping existing model.", len(rows)
        )
        print("⚠ Skipped retraining: not enough data. Existing model unchanged.")
        sys.exit(0)

    logger.info("Building feature matrix from %d rows...", len(rows))
    X = np.array([from_bq_row(r) for r in rows])
    logger.info("Feature matrix: %s", X.shape)

    logger.info("Retraining IsolationForest...")
    model = train_and_upload(
        X=X,
        gcs_path=GCS_MODEL_PATH,
        project_id=PROJECT_ID,
        local_path=LOCAL_MODEL,
    )

    print(f"\n✓ IsolationForest retrained on {len(X)} samples ({LOOKBACK_DAYS}-day window)")
    print(f"✓ Model uploaded → {GCS_MODEL_PATH}")
    print(f"  Features: {X.shape[1]}-dimensional")
    print(f"  Contamination: {model.contamination:.2f}")
    print(f"\nNote: Cloud Run instances will pick up the new model on next cold start.")
    print(f"To force a reload, redeploy the Cloud Run service.")


if __name__ == "__main__":
    main()
