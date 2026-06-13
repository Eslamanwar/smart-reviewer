"""Knowledge base retrieval — queries AWS Bedrock Knowledge Base."""

from __future__ import annotations

import logging
import os

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BEDROCK_KB_ID = os.getenv("BEDROCK_KB_ID", "")
BEDROCK_KB_AGENT_SLUG = os.getenv("BEDROCK_KB_AGENT_SLUG", "")
BEDROCK_KB_S3_URI_PREFIX = os.getenv("BEDROCK_KB_S3_URI_PREFIX", "")

_runtime_client = None


def _get_client():
    global _runtime_client
    if _runtime_client is None:
        _runtime_client = boto3.client("bedrock-agent-runtime")
    return _runtime_client


class _SearchInput(BaseModel):
    query: str = Field(description="Search query for this agent's Agent Knowledge.")


def _search(query: str) -> str:
    if not BEDROCK_KB_ID:
        return "Agent Knowledge is not configured for this agent."
    try:
        # The KB is shared across all agents; each agent's documents are
        # tagged with an ``agent_slug`` metadata attribute via sidecar files.
        # Filter by slug so retrieval only returns this agent's documents.
        vector_config = {"numberOfResults": 5}
        if BEDROCK_KB_AGENT_SLUG:
            vector_config["filter"] = {
                "equals": {
                    "key": "agent_slug",
                    "value": BEDROCK_KB_AGENT_SLUG,
                }
            }
        kwargs = {
            "knowledgeBaseId": BEDROCK_KB_ID,
            "retrievalQuery": {"text": query},
            "retrievalConfiguration": {
                "vectorSearchConfiguration": vector_config,
            },
        }
        resp = _get_client().retrieve(**kwargs)
        results = resp.get("retrievalResults", [])
        if not results:
            return "No relevant information found in Agent Knowledge."
        parts = []
        for i, r in enumerate(results, 1):
            text = r.get("content", {}).get("text", "")
            loc = r.get("location", {})
            uri = loc.get("s3Location", {}).get("uri", "")
            source = uri.rsplit("/", 1)[-1] if uri else ""
            entry = f"[{i}] {text}"
            if source:
                entry += f"\n    Source: {source}"
            parts.append(entry)
        return "\n\n".join(parts)
    except Exception as e:
        logger.error("Agent Knowledge query failed: %s", e)
        return f"Agent Knowledge query failed: {e}"


def build_knowledge_base_tool() -> StructuredTool | None:
    if not BEDROCK_KB_ID:
        logger.info("Agent Knowledge not configured — skipping tool")
        return None
    return StructuredTool.from_function(
        func=_search,
        name="search_knowledge_base",
        description=(
            "Search this agent's Agent Knowledge for relevant information. "
            "Agent Knowledge = files the agent owner uploaded specifically for "
            "THIS agent (distinct from Context Hub, which is org-wide). Use "
            "this for documents this agent's user is likely already familiar "
            "with — internal data, policies, brand guidelines, anything "
            "domain-specific to this agent."
        ),
        args_schema=_SearchInput,
    )
