#!/usr/bin/env python3
"""
RAG Agent Evaluation Runner — GCP (Vertex AI Search + Gemini ADK Agent)
------------------------------------------------------------------------
Evaluates the Cloud Run ADK agent endpoint against a fixed test suite.

Metrics:
  keyword_recall   (deterministic) — fraction of expected keywords found in the answer
  citation_recall  (deterministic) — expected source doc appeared in citations (0 or 1)
  latency_ms       (deterministic) — wall-clock time for the full agent response
  faithfulness     (LLM-as-judge)  — every claim grounded in cited sources (0–1)
  answer_relevance (LLM-as-judge)  — answer fully addresses the question (0–1)

Judge model: gemini-2.5-flash via Vertex AI (separate call from agent)

Pass thresholds (configurable via env vars):
  THRESHOLD_FAITHFULNESS    default 0.70
  THRESHOLD_RELEVANCE       default 0.75
  THRESHOLD_CITATION_RECALL default 0.60
  THRESHOLD_KEYWORD_RECALL  default 0.65
  THRESHOLD_P95_LATENCY_MS  default 10000  (ADK agents are slower than Lambda)

Usage:
  SERVICE_URL=https://... PROJECT_ID=my-project python3 scripts/run_evals.py
  SERVICE_URL=https://... PROJECT_ID=my-project python3 scripts/run_evals.py --no-judge
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVICE_URL = os.environ.get("SERVICE_URL", "").rstrip("/")
PROJECT_ID  = os.environ.get("PROJECT_ID", "")
REGION      = os.environ.get("REGION", "us-central1")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gemini-2.5-flash")
APP_NAME    = os.environ.get("ADK_APP_NAME", "agent")

THRESHOLDS = {
    "faithfulness":    float(os.environ.get("THRESHOLD_FAITHFULNESS",    "0.70")),
    "answer_relevance": float(os.environ.get("THRESHOLD_RELEVANCE",       "0.75")),
    "citation_recall": float(os.environ.get("THRESHOLD_CITATION_RECALL", "0.60")),
    "keyword_recall":  float(os.environ.get("THRESHOLD_KEYWORD_RECALL",  "0.65")),
    "p95_latency_ms":  float(os.environ.get("THRESHOLD_P95_LATENCY_MS",  "10000")),
}

EVAL_CASES_PATH = Path(__file__).parent.parent / "tests" / "eval_cases.json"
DEFAULT_OUTPUT  = Path(__file__).parent.parent / "eval_results.json"

# ---------------------------------------------------------------------------
# ADK agent caller
# ---------------------------------------------------------------------------

def _create_session(user_id: str) -> str:
    """Create a new ADK session and return the session ID."""
    resp = requests.post(
        f"{SERVICE_URL}/apps/{APP_NAME}/users/{user_id}/sessions",
        headers={"Content-Type": "application/json"},
        json={},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def call_agent(question: str) -> tuple[str, list[str], float]:
    """
    Run one turn against the ADK Cloud Run agent.
    Returns (answer, citations, latency_ms).
    Citations are filenames extracted from markdown links in the answer.
    """
    user_id = "eval"
    session_id = _create_session(user_id)

    body = {
        "appName": APP_NAME,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": {
            "role": "user",
            "parts": [{"text": question}],
        },
    }

    start = time.monotonic()
    resp = requests.post(
        f"{SERVICE_URL}/run",
        headers={"Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    latency_ms = (time.monotonic() - start) * 1000

    resp.raise_for_status()
    events = resp.json()

    # Collect text from all model-role events
    answer = ""
    for event in events:
        content = event.get("content", {})
        if content.get("role") == "model":
            for part in content.get("parts", []):
                if "text" in part:
                    answer += part["text"]

    # Extract filenames from Markdown citation links: [filename.txt](https://...)
    citations = re.findall(r"\[([^\]]+\.txt)\]", answer)

    return answer, citations, latency_ms

# ---------------------------------------------------------------------------
# Deterministic scorers
# ---------------------------------------------------------------------------

def score_keyword_recall(answer: str, expected_keywords: list[str]) -> float:
    if not expected_keywords:
        return 1.0
    answer_lower = answer.lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in answer_lower)
    return hits / len(expected_keywords)


def score_citation_recall(citations: list[str], expected_sources: list[str]) -> float:
    if not expected_sources:
        return 1.0
    citations_lower = [c.lower() for c in citations]
    for src in expected_sources:
        if src.lower() in citations_lower:
            return 1.0
    return 0.0

# ---------------------------------------------------------------------------
# LLM-as-judge scorer (Gemini 2.5 Flash via Vertex AI)
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """\
You are an expert evaluator for a RAG (Retrieval-Augmented Generation) system \
used in a manufacturing environment.

Question: {question}

Answer given: {answer}

Source documents cited: {citations}

Score the answer on BOTH of the following metrics using an integer from 1 to 5:

faithfulness — Are ALL factual claims in the answer directly supported by the \
cited source documents? No invented or extrapolated information.
  1 = significant fabrications present
  2 = several unsupported claims
  3 = mostly grounded, a few questionable details
  4 = nearly all claims traceable to sources
  5 = every claim is directly traceable to the cited sources

answer_relevance — Does the answer fully and directly address the question asked?
  1 = off-topic or does not address the question
  2 = tangentially related but misses the main point
  3 = partially addresses the question, missing key aspects
  4 = mostly complete, minor gaps
  5 = fully and directly addresses the question

Return ONLY a valid JSON object with exactly these keys — no markdown, no explanation outside the JSON:
{{"faithfulness": <integer 1-5>, "answer_relevance": <integer 1-5>, "reasoning": "<one sentence>"}}"""


def call_judge(question: str, answer: str, citations: list[str]) -> dict:
    """Call Gemini to score faithfulness and answer_relevance."""
    citation_str = ", ".join(citations) if citations else "none"
    prompt = JUDGE_PROMPT.format(
        question=question,
        answer=answer,
        citations=citation_str,
    )

    model = GenerativeModel(JUDGE_MODEL)
    response = model.generate_content(
        prompt,
        generation_config=GenerationConfig(temperature=0, max_output_tokens=512),
    )
    text = response.text.strip()

    # Strip accidental markdown fences
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    scores = json.loads(text)
    return {
        "faithfulness":    (scores["faithfulness"]    - 1) / 4,
        "answer_relevance": (scores["answer_relevance"] - 1) / 4,
        "reasoning": scores.get("reasoning", ""),
    }

# ---------------------------------------------------------------------------
# Reporting helpers (identical to AWS version)
# ---------------------------------------------------------------------------

def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return sorted_vals[idx]


def build_summary(cases: list[dict]) -> dict:
    def mean(key):
        vals = [c["scores"][key] for c in cases if c["scores"].get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    latencies = [c["latency_ms"] for c in cases]
    return {
        "faithfulness":    mean("faithfulness"),
        "answer_relevance": mean("answer_relevance"),
        "citation_recall": mean("citation_recall"),
        "keyword_recall":  mean("keyword_recall"),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "p95_latency_ms":  round(percentile(latencies, 95), 1),
    }


def check_thresholds(summary: dict, thresholds: dict) -> list[str]:
    failures = []
    for metric, threshold in thresholds.items():
        value = summary.get(metric, 0)
        if metric == "p95_latency_ms":
            if value > threshold:
                failures.append(f"{metric}: {value:.1f}ms > {threshold:.0f}ms threshold")
        else:
            if value < threshold:
                failures.append(f"{metric}: {value:.4f} < {threshold:.4f} threshold")
    return failures


def format_markdown_report(summary: dict, cases: list[dict], failures: list[str]) -> str:
    status = "✅ PASSED" if not failures else "❌ FAILED"
    lines = [
        f"## Eval Results — {status}",
        "",
        "### Summary",
        "",
        "| Metric | Score | Threshold | Status |",
        "|--------|-------|-----------|--------|",
    ]

    def metric_row(name, display_name, fmt="{:.4f}", higher_better=True):
        val = summary.get(name, 0)
        threshold = THRESHOLDS.get(name)
        if threshold is None:
            return
        ok = val >= threshold if higher_better else val <= threshold
        icon = "✅" if ok else "❌"
        lines.append(f"| {display_name} | {fmt.format(val)} | {fmt.format(threshold)} | {icon} |")

    metric_row("faithfulness",    "Faithfulness")
    metric_row("answer_relevance", "Answer Relevance")
    metric_row("citation_recall", "Citation Recall")
    metric_row("keyword_recall",  "Keyword Recall")
    metric_row("p95_latency_ms",  "p95 Latency (ms)", fmt="{:.0f}", higher_better=False)

    lines += [
        "",
        "### Per-Case Results",
        "",
        "| Case | Faithful | Relevant | Cite✓ | KW✓ | Latency |",
        "|------|----------|----------|-------|-----|---------|",
    ]
    for c in cases:
        s = c["scores"]
        if c.get("error"):
            lines.append(f"| {c['id']} | ERR | ERR | ERR | ERR | — |")
        else:
            lines.append(
                f"| {c['id']} "
                f"| {s['faithfulness']:.2f} "
                f"| {s['answer_relevance']:.2f} "
                f"| {'✅' if s['citation_recall'] == 1.0 else '❌'} "
                f"| {s['keyword_recall']:.2f} "
                f"| {c['latency_ms']:.0f}ms |"
            )

    if failures:
        lines += ["", "### Failures", ""]
        for f in failures:
            lines.append(f"- {f}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run GCP RAG agent evaluations")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-judge", action="store_true", help="Skip Gemini judge")
    args = parser.parse_args()

    if not SERVICE_URL:
        print("ERROR: SERVICE_URL environment variable is required", file=sys.stderr)
        print("  export SERVICE_URL=$(gcloud run services describe <name> \\", file=sys.stderr)
        print("    --region us-central1 --project <project> --format='value(status.url)')", file=sys.stderr)
        sys.exit(1)

    if not PROJECT_ID and not args.no_judge:
        print("ERROR: PROJECT_ID is required for the Gemini judge", file=sys.stderr)
        print("  export PROJECT_ID=<your-gcp-project-id>", file=sys.stderr)
        sys.exit(1)

    if not args.no_judge:
        vertexai.init(project=PROJECT_ID, location=REGION)

    eval_cases = json.loads(EVAL_CASES_PATH.read_text())
    print(f"Loaded {len(eval_cases)} eval cases")
    print(f"Agent:  {SERVICE_URL}")
    print(f"Judge:  {'DISABLED' if args.no_judge else JUDGE_MODEL} (Vertex AI)")
    print(f"Project:{PROJECT_ID}")
    print()

    results = []

    for i, case in enumerate(eval_cases, 1):
        print(f"[{i:2d}/{len(eval_cases)}] {case['id']} ... ", end="", flush=True)

        result = {
            "id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "answer": "",
            "citations": [],
            "latency_ms": 0,
            "scores": {},
        }

        try:
            answer, citations, latency_ms = call_agent(case["question"])
            result["answer"]    = answer
            result["citations"] = citations
            result["latency_ms"] = round(latency_ms, 1)

            scores = {
                "keyword_recall":  score_keyword_recall(answer, case["expected_keywords"]),
                "citation_recall": score_citation_recall(citations, case["expected_sources"]),
            }

            if not args.no_judge:
                judge_scores = call_judge(case["question"], answer, citations)
                scores["faithfulness"]     = round(judge_scores["faithfulness"], 4)
                scores["answer_relevance"] = round(judge_scores["answer_relevance"], 4)
                scores["judge_reasoning"]  = judge_scores["reasoning"]
            else:
                scores["faithfulness"]     = None
                scores["answer_relevance"] = None

            result["scores"] = scores

            kw   = f"kw={scores['keyword_recall']:.2f}"
            cite = f"cite={'✅' if scores['citation_recall'] == 1.0 else '❌'}"
            if not args.no_judge:
                print(f"{kw} {cite} faith={scores['faithfulness']:.2f} rel={scores['answer_relevance']:.2f} {latency_ms:.0f}ms")
            else:
                print(f"{kw} {cite} {latency_ms:.0f}ms")

        except Exception as e:
            result["error"] = str(e)
            result["scores"] = {
                "keyword_recall": 0.0, "citation_recall": 0.0,
                "faithfulness": 0.0, "answer_relevance": 0.0,
            }
            print(f"ERROR: {e}")

        results.append(result)

    summary  = build_summary(results)
    failures = check_thresholds(summary, THRESHOLDS) if not args.no_judge else []
    passed   = len(failures) == 0

    output = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "service_url":   SERVICE_URL,
        "judge_model":   JUDGE_MODEL if not args.no_judge else None,
        "thresholds":    THRESHOLDS,
        "passed":        passed,
        "summary":       summary,
        "cases":         results,
    }
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"\nResults saved → {args.output}")

    report = format_markdown_report(summary, results, failures)
    print("\n" + report)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(report + "\n")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
