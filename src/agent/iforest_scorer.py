"""
IsolationForest scorer for the agent container.

Loads a pre-trained model from GCS on first call (lazy, cached).
Falls back gracefully if the model doesn't exist yet (cold start or first deploy).
"""
from __future__ import annotations

import io
import logging
import math
import os
import pickle
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

GCS_MODEL_PATH = os.environ.get("GCS_MODEL_PATH", "")  # gs://bucket/models/iforest.pkl
_model = None
_load_lock = threading.Lock()

FEATURE_NAMES = [
    "retrieval_score_mean",
    "retrieval_score_std",
    "retrieval_score_entropy",
    "chunk_count",
    "reranker_score_mean",
    "search_latency_ms",
]


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _load_lock:
        if _model is not None:
            return _model
        if not GCS_MODEL_PATH:
            logger.info("GCS_MODEL_PATH not set — IForest scoring disabled")
            return None
        try:
            from google.cloud import storage
            project_id = os.environ.get("PROJECT_ID", "")
            client = storage.Client(project=project_id)
            bucket_name, blob_path = GCS_MODEL_PATH.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            data = blob.download_as_bytes()
            _model = pickle.loads(data)
            logger.info("IForest model loaded from %s", GCS_MODEL_PATH)
        except Exception as exc:
            logger.warning("IForest model load failed (non-fatal): %s", exc)
            _model = None
    return _model


def _entropy(scores: list[float]) -> float:
    if not scores or sum(scores) == 0:
        return 0.0
    total = sum(scores)
    probs = [s / total for s in scores]
    return -sum(p * math.log(p + 1e-10) for p in probs)


def build_features(
    retrieval_scores: list[float],
    reranker_scores: list[float],
    search_latency_ms: float,
) -> np.ndarray:
    """Build the 6-feature vector used for runtime anomaly scoring."""
    if not retrieval_scores:
        retrieval_scores = [0.0]
    if not reranker_scores:
        reranker_scores = [0.0]
    return np.array([
        float(np.mean(retrieval_scores)),
        float(np.std(retrieval_scores)) if len(retrieval_scores) > 1 else 0.0,
        _entropy(retrieval_scores),
        float(len(retrieval_scores)),
        float(np.mean(reranker_scores)),
        search_latency_ms,
    ], dtype=np.float64)


def score(
    retrieval_scores: list[float],
    reranker_scores: list[float],
    search_latency_ms: float,
) -> tuple[float, bool]:
    """
    Returns (anomaly_score, is_anomaly).
    anomaly_score: IForest decision_function output (negative = more anomalous).
    is_anomaly: True when IForest predicts -1 (outlier).
    Returns (0.0, False) gracefully if no model is available.
    """
    model = _load_model()
    if model is None:
        return 0.0, False
    try:
        x = build_features(retrieval_scores, reranker_scores, search_latency_ms).reshape(1, -1)
        # decision_function: higher = more normal; score < 0 usually means anomaly
        decision = float(model.decision_function(x)[0])
        label = int(model.predict(x)[0])  # 1 = normal, -1 = anomaly
        return decision, label == -1
    except Exception as exc:
        logger.warning("IForest scoring failed (non-fatal): %s", exc)
        return 0.0, False
