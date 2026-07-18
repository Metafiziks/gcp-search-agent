"""
IsolationForest wrapper — train, persist to GCS, load for scoring.

scikit-learn IsolationForest learns the distribution of "healthy" requests
and assigns anomaly scores to new observations. Negative decision_function
values indicate increasingly anomalous behaviour.

Usage (training):
    from observability.isolation_forest import train_and_upload
    train_and_upload(X, gcs_path="gs://bucket/models/iforest.pkl")

Usage (scoring, same interface as agent/iforest_scorer.py):
    model = load_from_gcs("gs://bucket/models/iforest.pkl")
    score, is_anomaly = score_features(model, feature_vector)
"""
from __future__ import annotations

import io
import logging
import os
import pickle
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

CONTAMINATION = float(os.environ.get("IFOREST_CONTAMINATION", "0.05"))


def train(X: np.ndarray, contamination: float = CONTAMINATION) -> IsolationForest:
    """Fit an IsolationForest on feature matrix X (n_samples × n_features)."""
    if len(X) < 10:
        raise ValueError(f"Need at least 10 samples to train, got {len(X)}")
    model = IsolationForest(
        n_estimators=100,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X)
    logger.info(
        "IForest trained: %d samples, %d features, contamination=%.2f",
        len(X), X.shape[1], contamination,
    )
    return model


def save_local(model: IsolationForest, path: str) -> None:
    with open(path, "wb") as f:
        pickle.dump(model, f)
    logger.info("IForest model saved locally: %s", path)


def upload_to_gcs(local_path: str, gcs_path: str, project_id: str) -> None:
    from google.cloud import storage
    client = storage.Client(project=project_id)
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    client.bucket(bucket_name).blob(blob_path).upload_from_filename(local_path)
    logger.info("IForest model uploaded: %s", gcs_path)


def load_from_gcs(gcs_path: str, project_id: str) -> Optional[IsolationForest]:
    try:
        from google.cloud import storage
        client = storage.Client(project=project_id)
        bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
        data = client.bucket(bucket_name).blob(blob_path).download_as_bytes()
        model = pickle.loads(data)
        logger.info("IForest model loaded from GCS: %s", gcs_path)
        return model
    except Exception as exc:
        logger.warning("Could not load IForest from GCS: %s", exc)
        return None


def train_and_upload(
    X: np.ndarray,
    gcs_path: str,
    project_id: str,
    local_path: str = "/tmp/iforest.pkl",
    contamination: float = CONTAMINATION,
) -> IsolationForest:
    """Train, save locally, upload to GCS. Returns the trained model."""
    model = train(X, contamination=contamination)
    save_local(model, local_path)
    upload_to_gcs(local_path, gcs_path, project_id)
    return model


def score_features(
    model: IsolationForest,
    features: np.ndarray,
) -> tuple[float, bool]:
    """
    Score a single feature vector.
    Returns (decision_score, is_anomaly).
      decision_score < 0 → anomalous territory
      is_anomaly=True → IForest predicts outlier
    """
    x = features.reshape(1, -1)
    decision = float(model.decision_function(x)[0])
    label    = int(model.predict(x)[0])  # 1=normal, -1=anomaly
    return decision, label == -1
