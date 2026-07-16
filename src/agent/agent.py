import logging
import os

from google.adk.agents import Agent
from google.cloud import discoveryengine_v1beta as discoveryengine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are a manufacturing documentation assistant. You answer questions ONLY
using information retrieved from the organization's procedure documents.

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


def search_knowledge_base(query: str) -> str:
    """Search the organization's knowledge base documents.

    Args:
        query: The search query string.

    Returns:
        Relevant document excerpts with source links, or a message if nothing is found.
    """
    project_id = os.environ["PROJECT_ID"]
    datastore_id = os.environ["SEARCH_DATASTORE_ID"]
    engine_id = os.environ.get("SEARCH_ENGINE_ID", datastore_id.replace("-docs", "-search"))
    location = os.environ.get("SEARCH_LOCATION", "global")

    client = discoveryengine.SearchServiceClient()
    serving_config = (
        f"projects/{project_id}/locations/{location}"
        f"/collections/default_collection/engines/{engine_id}"
        f"/servingConfigs/default_search"
    )
    try:
        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            page_size=5,
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

    excerpts = []
    for result in response.results:
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
        link = data.get("link", "")
        if link.startswith("gs://"):
            link = link.replace("gs://", "https://storage.googleapis.com/", 1)

        header = f"[{title}]({link})" if link else f"[{title}]"
        excerpts.append(f"{header}\n{content}" if content else header)

    return "\n\n---\n\n".join(excerpts) if excerpts else "No relevant documents found."


root_agent = Agent(
    name="search_agent",
    model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    instruction=INSTRUCTIONS,
    tools=[search_knowledge_base],
)
