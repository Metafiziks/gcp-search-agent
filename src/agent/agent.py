import logging
import math
import os
import sys
import time
import uuid

# Ensure sibling modules (bigquery_sink, iforest_scorer) are importable
# when ADK deploys this file into /app/agents/agent/ inside Cloud Run
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google.adk.agents import Agent
from google.cloud import discoveryengine_v1beta as discoveryengine

import bigquery_sink
import iforest_scorer
import memory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Observability: Vertex AI Ranking API ─────────────────────────────────────

def _rerank(project_id: str, query: str, records: list[tuple[str, str]]) -> list[tuple[str, str, float]]:
    """
    Re-rank (id, content) pairs using Vertex AI's semantic ranker.
    Returns [(id, content, score), ...] sorted by score descending.
    Falls back to original order if the API is unavailable.
    """
    if not records:
        return []
    try:
        rank_client = discoveryengine.RankServiceClient()
        ranking_config = (
            f"projects/{project_id}/locations/global"
            "/rankingConfigs/default_ranking_config"
        )
        ranking_records = [
            discoveryengine.RankingRecord(id=rid, content=content)
            for rid, content in records
        ]
        response = rank_client.rank(
            request=discoveryengine.RankRequest(
                ranking_config=ranking_config,
                model="semantic-ranker-512@latest",
                top_n=len(records),
                query=query,
                records=ranking_records,
            )
        )
        id_to_orig = {rid: content for rid, content in records}
        return [
            (r.id, id_to_orig[r.id], r.score)
            for r in response.records
        ]
    except Exception as exc:
        logger.warning("Reranker unavailable, using original order: %s", exc)
        return [(rid, content, 1.0 / (i + 1)) for i, (rid, content) in enumerate(records)]

BASE_INSTRUCTIONS = """
You are a manufacturing documentation assistant. Answer manufacturing
procedure, safety, maintenance, and quality questions ONLY using information
retrieved from the organization's procedure documents.

STRICT RULES — follow these without exception:
1. If the knowledge base returns no relevant documents, respond with exactly:
   "I couldn't find information about that in the available documentation.
   Please check with your supervisor or try rephrasing your question with
   more specific terms (e.g. include the equipment name)."
2. Never use general knowledge, training data, or assumptions to fill gaps.
3. Never ask the user clarifying questions. Either answer from retrieved
   documents, or return the not-found message above.
4. Never speculate or suggest what the answer "might" be.

Answering style (when documents are retrieved):
- Synthesize information in your own words — do not quote documents verbatim.
- For procedures or steps, present them clearly in order.
- Always cite your sources at the end: [Document Name](url)
- Only cite documents you actually used to answer the question.
"""

MEMORY_INSTRUCTIONS = """

Memory layer:
- Use memory only for scoped user/session continuity such as a user's line,
  role, preferences, or prior context. Do not use memory as a source for
  manufacturing procedures, requirements, safety rules, or quality standards.
- At the start of a user turn that depends on prior user/session context, call
  recall_user_memory before deciding whether document retrieval is needed.
- When a user explicitly gives durable context or a preference to remember,
  call save_user_memory with a short topic and concise content.
- Keep document retrieval separate: call search_knowledge_base for policy,
  procedure, maintenance, safety, or quality facts. Memory must never replace
  citations from the document corpus.
- If answering only from remembered user/session context, say "You told me..."
  or "For this session..." and do not cite procedure documents.
"""

INSTRUCTIONS = BASE_INSTRUCTIONS + (MEMORY_INSTRUCTIONS if memory.is_enabled() else "")


def search_knowledge_base(query: str) -> str:
    """Search the organization's knowledge base documents.

    Args:
        query: The search query string.

    Returns:
        Relevant document excerpts re-ranked by semantic relevance,
        with source links, or a message if nothing is found.
    """
    project_id = os.environ["PROJECT_ID"]
    datastore_id = os.environ["SEARCH_DATASTORE_ID"]
    engine_id = os.environ.get("SEARCH_ENGINE_ID", datastore_id.replace("-docs", "-search"))
    location = os.environ.get("SEARCH_LOCATION", "global")
    request_id = str(uuid.uuid4())

    client = discoveryengine.SearchServiceClient()
    serving_config = (
        f"projects/{project_id}/locations/{location}"
        f"/collections/default_collection/engines/{engine_id}"
        f"/servingConfigs/default_search"
    )

    search_start = time.monotonic()
    try:
        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            page_size=8,  # fetch more so the reranker has candidates to select from
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True,
                    max_snippet_count=3,
                ),
                extractive_content_spec=discoveryengine.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
                    max_extractive_answer_count=3,
                    max_extractive_segment_count=3,
                ),
            ),
        )
        response = client.search(request)
    except Exception as exc:
        logger.error("Search error: %s", exc)
        return f"Search unavailable: {exc}"
    search_latency_ms = (time.monotonic() - search_start) * 1000

    # ── Extract chunks and raw relevance scores ───────────────────────────────
    raw_chunks = []   # [(doc_id, content, title, link, rel_score)]
    for i, result in enumerate(response.results):
        data = result.document.derived_struct_data
        content = ""
        if "extractive_answers" in data and data["extractive_answers"]:
            content = " ".join(
                a.get("content", "") for a in data["extractive_answers"] if a.get("content")
            )
        if not content and "extractive_segments" in data and data["extractive_segments"]:
            content = " ".join(
                s.get("content", "") for s in data["extractive_segments"] if s.get("content")
            )
        if not content and "snippets" in data and data["snippets"]:
            content = " ".join(
                s.get("snippet", "") for s in data["snippets"] if s.get("snippet")
            )
        title = data.get("title", "") or result.document.id
        link  = data.get("link", "")
        if link.startswith("gs://"):
            link = link.replace("gs://", "https://storage.googleapis.com/", 1)
        # Use relevance_score from Enterprise tier; fall back to rank-based score
        rel_score = getattr(result, "relevance_score", None) or 1.0 / (i + 1)
        raw_chunks.append((result.document.id, content, title, link, float(rel_score)))

    if not raw_chunks:
        return "No relevant documents found."

    retrieval_scores = [c[4] for c in raw_chunks]

    # ── Vertex AI Ranking API: semantic re-ranking ────────────────────────────
    rerank_input = [(c[0], f"{c[2]}: {c[1]}"[:512]) for c in raw_chunks if c[1]]
    reranked = _rerank(project_id, query, rerank_input)
    reranker_scores = [r[2] for r in reranked]

    chunk_by_id = {c[0]: c for c in raw_chunks}
    top_ids = [r[0] for r in reranked[:5]] if reranked else [c[0] for c in raw_chunks[:5]]

    # ── Anomaly detection ─────────────────────────────────────────────────────
    anomaly_score, is_anomaly = iforest_scorer.score(
        retrieval_scores=retrieval_scores,
        reranker_scores=reranker_scores,
        search_latency_ms=search_latency_ms,
    )
    if is_anomaly:
        logger.warning(
            "Anomalous retrieval [request_id=%s]: iforest=%.4f chunks=%d latency=%.0fms",
            request_id, anomaly_score, len(retrieval_scores), search_latency_ms,
        )

    # ── Telemetry (non-blocking) ──────────────────────────────────────────────
    n = len(retrieval_scores)
    mean_r = sum(retrieval_scores) / n
    std_r  = math.sqrt(sum((s - mean_r) ** 2 for s in retrieval_scores) / n)
    total  = sum(retrieval_scores) or 1e-10
    entropy_r = -sum((s / total) * math.log(s / total + 1e-10) for s in retrieval_scores)

    bigquery_sink.log_async(
        request_id=request_id,
        query=query,
        source="runtime",
        search_latency_ms=search_latency_ms,
        retrieval_score_mean=mean_r,
        retrieval_score_std=std_r,
        retrieval_score_entropy=entropy_r,
        chunk_count=n,
        reranker_score_mean=sum(reranker_scores) / len(reranker_scores) if reranker_scores else 0.0,
        anomaly_score=anomaly_score,
        is_anomaly=is_anomaly,
        memory_enabled=memory.is_enabled(),
        memory_backend=memory.backend_name(),
    )

    # ── Build context from re-ranked top-5 ───────────────────────────────────
    excerpts = []
    for doc_id in top_ids:
        if doc_id not in chunk_by_id:
            continue
        _, content, title, link, _ = chunk_by_id[doc_id]
        header = f"[{title}]({link})" if link else f"[{title}]"
        excerpts.append(f"{header}\n{content}" if content else header)

    return "\n\n---\n\n".join(excerpts) if excerpts else "No relevant documents found."


TOOLS = [search_knowledge_base]
if memory.is_enabled():
    TOOLS.extend([memory.recall_user_memory, memory.save_user_memory])


root_agent = Agent(
    name="search_agent",
    model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    instruction=INSTRUCTIONS,
    tools=TOOLS,
)
