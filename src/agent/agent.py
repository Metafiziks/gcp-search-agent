import logging
import os

from google.adk.agents import Agent
from google.cloud import discoveryengine_v1beta as discoveryengine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are a knowledgeable assistant that answers questions based on the organization's
documents and procedures.

**Answering style:**
- Synthesize and summarize information in your own words — do not quote documents verbatim.
- When procedures or steps are involved, present them clearly in order.
- If the knowledge base does not contain the answer, say so — never guess or use general knowledge.

**Citations:**
- Always cite your sources at the end of your response.
- Format each citation as a markdown link: [Document Name](url)
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
    location = os.environ.get("SEARCH_LOCATION", "global")

    client = discoveryengine.SearchServiceClient()
    serving_config = (
        f"projects/{project_id}/locations/{location}"
        f"/collections/default_collection/dataStores/{datastore_id}"
        f"/servingConfigs/default_config"
    )

    try:
        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            page_size=5,
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True,
                    max_snippet_count=2,
                ),
                extractive_content_spec=discoveryengine.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
                    max_extractive_answer_count=1,
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
            content = data["extractive_answers"][0].get("content", "")
        if not content and "snippets" in data and data["snippets"]:
            content = data["snippets"][0].get("snippet", "")

        title = data.get("title", "") or result.document.id
        link = data.get("link", "")

        header = f"[{title}]({link})" if link else f"[{title}]"
        excerpts.append(f"{header}\n{content}" if content else header)

    return "\n\n---\n\n".join(excerpts) if excerpts else "No relevant documents found."


root_agent = Agent(
    name="search_agent",
    model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
    instruction=INSTRUCTIONS,
    tools=[search_knowledge_base],
)
