"""Cloud Run entrypoint for protected mode (KB_ACCESS_MODE=protected)."""

import logging
import os

import httpx
from google import genai
from google.genai import types

from protected_host import build_app
from protected_search import ProtectedSearch

logging.basicConfig(level=logging.INFO)

project = os.environ["PROJECT_ID"]
# gemini-2.5-* were retired on 2026-10-20. Current GA Gemini models are served from the global endpoint.
model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
http = httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)
search = ProtectedSearch(
    project=project,
    engine=os.environ["SEARCH_ENGINE_ID"],
    datastore=os.environ["SEARCH_DATASTORE_ID"],
    bucket=os.environ["DOCS_BUCKET"],
    http=http,
    location=os.environ.get("SEARCH_LOCATION", "global"),
)
client = genai.Client(vertexai=True, project=project, location=os.environ.get("GEMINI_LOCATION", "global"))


async def generate(instructions: str, payload: str) -> str:
    # One stateless call: no tools, no session, no memory, no stored conversation.
    response = await client.aio.models.generate_content(
        model=model,
        contents=payload,
        config=types.GenerateContentConfig(system_instruction=instructions, temperature=0),
    )
    return response.text or ""


app = build_app(search, generate)
