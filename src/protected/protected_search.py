"""Stateless retrieval from one ACL-enabled Agent Search (Vertex AI Search) data store.

The search call is made with the signed-in END USER's OAuth access token, never with
the agent's service account. Agent Search evaluates each document's ``acl_info``
against that identity and returns only documents the user may read. This module
performs no access decisions of its own: it checks that it is pointed at the intended
protected store (fail closed) and passes through only what the platform returned.
"""

import asyncio
import logging
import re
from urllib.parse import urlsplit

import google.auth
import httpx
from google.auth.transport.requests import Request as AuthRequest

API = "https://discoveryengine.googleapis.com/v1"
MAX_EXCERPT = 6000
MAX_RESULTS = 5
logger = logging.getLogger(__name__)


class ProtectedSearchError(RuntimeError):
    """Protected retrieval could not be completed safely."""


class UserCredentialRejected(ProtectedSearchError):
    """Search refused the user's credential (missing permission, invalid or expired token)."""

    def __init__(self, status: int) -> None:
        super().__init__("The user's credential was rejected by Search")
        self.status = status


def valid_token(token: object) -> bool:
    return (
        isinstance(token, str)
        and 20 <= len(token) <= 4096
        and all(33 <= ord(char) <= 126 for char in token)
    )


def validate_names(project: str, location: str, engine: str, datastore: str, bucket: str) -> None:
    for label, value, pattern in (
        ("project", project, r"[a-z][a-z0-9-]{4,28}[a-z0-9]"),
        ("location", location, r"global|us|eu"),
        ("engine", engine, r"[a-z0-9][a-z0-9_-]{0,62}"),
        ("data store", datastore, r"[a-z0-9][a-z0-9_-]{0,62}"),
        ("bucket", bucket, r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]"),
    ):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"Invalid {label}")


def is_protected_store(store: object, store_id: str) -> bool:
    return (
        isinstance(store, dict)
        and str(store.get("name", "")).endswith(f"/dataStores/{store_id}")
        and store.get("aclEnabled") is True
        and store.get("industryVertical") == "GENERIC"
        and store.get("contentConfig") == "CONTENT_REQUIRED"
    )


def serves_only(engine: object, engine_id: str, store_id: str) -> bool:
    return (
        isinstance(engine, dict)
        and str(engine.get("name", "")).endswith(f"/engines/{engine_id}")
        and engine.get("dataStoreIds") == [store_id]
    )


def source_name(link: object, bucket: str) -> str | None:
    """Return the filename only for objects in the protected bucket, else None."""
    if not isinstance(link, str):
        return None
    parts = urlsplit(link)
    if parts.scheme != "gs" or parts.netloc != bucket:
        return None
    name = parts.path.lstrip("/").split("/")[-1]
    return name or None


def _text(data: dict) -> str:
    for key, field in (("extractive_answers", "content"), ("extractive_segments", "content"), ("snippets", "snippet")):
        items = data.get(key)
        if isinstance(items, list):
            text = " ".join(i[field] for i in items if isinstance(i, dict) and isinstance(i.get(field), str) and i[field])
            if text:
                return text
    return ""


class ProtectedSearch:
    def __init__(
        self, project: str, engine: str, datastore: str, bucket: str,
        http: httpx.AsyncClient, location: str = "global",
    ) -> None:
        validate_names(project, location, engine, datastore, bucket)
        self.project, self.engine, self.datastore, self.bucket = project, engine, datastore, bucket
        self.http = http
        base = f"{API}/projects/{project}/locations/{location}"
        self.store_url = f"{base}/collections/default_collection/dataStores/{datastore}"
        self.engine_url = f"{base}/collections/default_collection/engines/{engine}"
        self.serving_url = f"{self.engine_url}/servingConfigs/default_search:search"
        self._credentials = None

    async def _runtime_token(self) -> str:
        def fetch() -> str:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if not self._credentials.valid:
                self._credentials.refresh(AuthRequest())
            return self._credentials.token

        return await asyncio.to_thread(fetch)

    async def verify_config(self) -> None:
        """Fail closed unless the configured store is ACL-enabled and is the engine's only store.

        Uses the runtime identity, which can read this metadata but cannot read documents.
        """
        headers = {"Authorization": f"Bearer {await self._runtime_token()}", "x-goog-user-project": self.project}
        try:
            store = await self.http.get(self.store_url, headers=headers)
            engine = await self.http.get(self.engine_url, headers=headers)
            store.raise_for_status()
            engine.raise_for_status()
            store_body, engine_body = store.json(), engine.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Protected configuration check failed (%s)", type(exc).__name__)
            raise ProtectedSearchError("Protected search configuration could not be verified") from None
        if not is_protected_store(store_body, self.datastore):
            raise ProtectedSearchError("The data store is not an ACL-enabled protected store")
        if not serves_only(engine_body, self.engine, self.datastore):
            raise ProtectedSearchError("The search engine must serve only the protected data store")

    async def excerpts(self, question: str, user_token: str) -> list[dict[str, str]]:
        if not valid_token(user_token):
            raise UserCredentialRejected(401)
        await self.verify_config()
        try:
            response = await self.http.post(
                self.serving_url,
                headers={
                    "Authorization": f"Bearer {user_token}",  # the END USER, not the agent
                    "x-goog-user-project": self.project,
                },
                json={
                    "query": question,
                    "pageSize": MAX_RESULTS,
                    "contentSearchSpec": {
                        "snippetSpec": {"returnSnippet": True},
                        "extractiveContentSpec": {"maxExtractiveAnswerCount": 1, "maxExtractiveSegmentCount": 3},
                    },
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("Protected search request failed (%s)", type(exc).__name__)
            raise ProtectedSearchError("Protected document retrieval failed") from None
        if response.status_code in (401, 403):
            logger.warning("Search rejected the user credential (HTTP %d)", response.status_code)
            raise UserCredentialRejected(response.status_code)
        if response.status_code != 200:
            logger.warning("Search returned HTTP %d", response.status_code)
            raise ProtectedSearchError("Protected document retrieval failed")
        try:
            body = response.json()
            results = body.get("results", [])
        except (ValueError, AttributeError):
            raise ProtectedSearchError("Invalid protected search response") from None
        if not isinstance(results, list):
            raise ProtectedSearchError("Invalid protected search response")

        documents = []
        for result in results[:MAX_RESULTS]:
            document = result.get("document") if isinstance(result, dict) else None
            data = document.get("derivedStructData") if isinstance(document, dict) else None
            if not isinstance(data, dict):
                continue
            # Ground only on references that come from the protected bucket.
            name = source_name(data.get("link"), self.bucket)
            text = _text(data)
            if name and text:
                documents.append({"source": name, "excerpt": text[:MAX_EXCERPT]})
        return documents
