from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
import uuid
from typing import Any

import bigquery_sink

try:
    from google.adk.tools.tool_context import ToolContext
except ImportError:  # pragma: no cover - ADK is present in the deployed agent.
    ToolContext = Any

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be >= 0, got {parsed}")
    return parsed


MEMORY_ENABLED = _env_bool("MEMORY_ENABLED", False)
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "session_state").strip().lower()
MEMORY_SCOPE = os.environ.get("MEMORY_SCOPE", "session").strip().lower()
MEMORY_OPTIONAL = _env_bool("MEMORY_OPTIONAL", True)
MEMORY_MAX_ITEMS = _env_int("MEMORY_MAX_ITEMS", 20)
MEMORY_TTL_SECONDS = _env_int("MEMORY_TTL_SECONDS", 0)
MEMORY_NAMESPACE = os.environ.get("MEMORY_NAMESPACE", "manufacturing_assistant").strip()

SUPPORTED_BACKENDS = {"session_state", "adk_memory", "memory_bank"}
SUPPORTED_SCOPES = {"session", "user"}

if MEMORY_SCOPE not in SUPPORTED_SCOPES:
    raise ValueError(f"MEMORY_SCOPE must be one of {sorted(SUPPORTED_SCOPES)}, got {MEMORY_SCOPE!r}")
if MEMORY_BACKEND not in SUPPORTED_BACKENDS:
    raise ValueError(f"MEMORY_BACKEND must be one of {sorted(SUPPORTED_BACKENDS)}, got {MEMORY_BACKEND!r}")
if not MEMORY_NAMESPACE:
    raise ValueError("MEMORY_NAMESPACE must not be empty")


def is_enabled() -> bool:
    return MEMORY_ENABLED


def backend_name() -> str:
    return MEMORY_BACKEND if MEMORY_ENABLED else "disabled"


def _state_key() -> str:
    prefix = "user:" if MEMORY_SCOPE == "user" else ""
    return f"{prefix}{MEMORY_NAMESPACE}:memory"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _normalize_text(value: str, *, max_length: int = 1000) -> str:
    return re.sub(r"\s+", " ", value).strip()[:max_length]


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2}


def _emit_memory_telemetry(
    *,
    query: str,
    read_count: int,
    write_count: int,
    latency_ms: float,
) -> None:
    bigquery_sink.log_memory_async(
        request_id=str(uuid.uuid4()),
        query=query,
        memory_enabled=MEMORY_ENABLED,
        memory_backend=backend_name(),
        memory_read_count=read_count,
        memory_write_count=write_count,
        memory_latency_ms=latency_ms,
    )


def _unavailable(message: str) -> str:
    logger.warning(message)
    if not MEMORY_OPTIONAL:
        raise RuntimeError(message)
    return message


def _get_session_items(tool_context: ToolContext) -> list[dict[str, Any]]:
    if tool_context is None or not hasattr(tool_context, "state"):
        raise RuntimeError("ADK ToolContext with state is required for session_state memory")
    raw_items = tool_context.state.get(_state_key(), [])
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise RuntimeError(f"Memory state key {_state_key()!r} is not a list")
    return [item for item in raw_items if isinstance(item, dict)]


def _set_session_items(tool_context: ToolContext, items: list[dict[str, Any]]) -> None:
    tool_context.state[_state_key()] = items


def _prune_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pruned = items
    if MEMORY_TTL_SECONDS > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=MEMORY_TTL_SECONDS)
        kept: list[dict[str, Any]] = []
        for item in pruned:
            created_at = item.get("created_at")
            if not created_at:
                kept.append(item)
                continue
            try:
                if dt.datetime.fromisoformat(created_at) >= cutoff:
                    kept.append(item)
            except ValueError:
                kept.append(item)
        pruned = kept
    if MEMORY_MAX_ITEMS > 0:
        pruned = pruned[-MEMORY_MAX_ITEMS:]
    return pruned


def _format_items(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No memory found for this user/session."
    lines = []
    for item in items:
        topic = item.get("topic") or "general"
        content = item.get("content") or ""
        lines.append(f"- {topic}: {content}")
    return "\n".join(lines)


def _recall_session_state(query: str, tool_context: ToolContext) -> list[dict[str, Any]]:
    items = _prune_items(_get_session_items(tool_context))
    _set_session_items(tool_context, items)
    query_tokens = _tokens(query)
    if not query_tokens:
        return items[-5:]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        haystack = f"{item.get('topic', '')} {item.get('content', '')}"
        overlap = len(query_tokens & _tokens(haystack))
        if overlap:
            scored.append((overlap, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:5]]


def _save_session_state(topic: str, content: str, tool_context: ToolContext) -> None:
    items = _prune_items(_get_session_items(tool_context))
    items.append(
        {
            "topic": topic,
            "content": content,
            "created_at": _utc_now(),
            "scope": MEMORY_SCOPE,
        }
    )
    _set_session_items(tool_context, _prune_items(items))


def _recall_adk_memory(query: str, tool_context: ToolContext) -> str:
    if tool_context is None or not hasattr(tool_context, "search_memory"):
        return _unavailable("ADK memory is unavailable because ToolContext.search_memory is missing")
    response = tool_context.search_memory(query)
    memories = getattr(response, "memories", []) or []
    formatted = []
    for memory in memories[:5]:
        content = getattr(memory, "content", None)
        parts = getattr(content, "parts", []) if content else []
        text = " ".join(getattr(part, "text", "") for part in parts if getattr(part, "text", ""))
        if text:
            formatted.append({"topic": "adk_memory", "content": text})
    return _format_items(formatted)


def _save_adk_memory(topic: str, content: str, tool_context: ToolContext) -> None:
    if tool_context is None or not hasattr(tool_context, "add_memory"):
        _unavailable("ADK memory is unavailable because ToolContext.add_memory is missing")
        return
    try:
        from google.adk.memory.memory_entry import MemoryEntry
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("ADK memory dependencies are not installed") from exc
    entry = MemoryEntry(
        content=types.Content(
            role="user",
            parts=[types.Part(text=f"{topic}: {content}")],
        ),
        custom_metadata={
            "scope": MEMORY_SCOPE,
            "namespace": MEMORY_NAMESPACE,
        },
    )
    tool_context.add_memory(memories=[entry])


def _memory_bank_unavailable() -> str:
    return _unavailable(
        "Vertex AI Agent Engine Memory Bank is not enabled by this Cloud Run template. "
        "Use MEMORY_BACKEND=session_state for Cloud Run short-term memory, or deploy the "
        "agent through Vertex AI Agent Engine and configure an ADK memory service before "
        "using MEMORY_BACKEND=memory_bank."
    )


def recall_user_memory(query: str, tool_context: ToolContext) -> str:
    """Recall scoped user/session memory without querying the document corpus."""
    started = time.monotonic()
    read_count = 0
    normalized_query = _normalize_text(query, max_length=300)
    try:
        if not MEMORY_ENABLED:
            return "Memory is disabled."
        if MEMORY_BACKEND == "memory_bank":
            return _memory_bank_unavailable()
        if MEMORY_BACKEND == "adk_memory":
            result = _recall_adk_memory(normalized_query, tool_context)
            read_count = 0 if result.startswith("No memory found") else 1
            return result
        matches = _recall_session_state(normalized_query, tool_context)
        read_count = len(matches)
        return _format_items(matches)
    finally:
        _emit_memory_telemetry(
            query=normalized_query,
            read_count=read_count,
            write_count=0,
            latency_ms=(time.monotonic() - started) * 1000,
        )


def save_user_memory(topic: str, content: str, tool_context: ToolContext) -> str:
    """Save scoped user/session context or preferences for later turns."""
    started = time.monotonic()
    write_count = 0
    normalized_topic = _normalize_text(topic, max_length=120) or "general"
    normalized_content = _normalize_text(content)
    try:
        if not MEMORY_ENABLED:
            return "Memory is disabled."
        if not normalized_content:
            raise ValueError("content must not be empty")
        if MEMORY_BACKEND == "memory_bank":
            return _memory_bank_unavailable()
        if MEMORY_BACKEND == "adk_memory":
            _save_adk_memory(normalized_topic, normalized_content, tool_context)
        else:
            _save_session_state(normalized_topic, normalized_content, tool_context)
        write_count = 1
        return f"Saved memory for {MEMORY_SCOPE} scope: {normalized_topic}."
    finally:
        _emit_memory_telemetry(
            query=f"{normalized_topic}: {normalized_content}",
            read_count=0,
            write_count=write_count,
            latency_ms=(time.monotonic() - started) * 1000,
        )
