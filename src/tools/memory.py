"""Persistent memory — semantic recall from AgentCore Memory.

The agent's chat turns are saved to AgentCore Memory by the BFF after every
stream completes. The configured ``semantic`` strategy (namespace = agent
slug) extracts facts asynchronously, then this tool exposes a search over
those facts so the LLM can recall things from past sessions across all
users of this agent.

Writes are deliberately *not* exposed as a tool: the BFF already records
every turn, and the strategy extracts what matters. Adding a `remember()`
tool would duplicate work and produce noisy, low-signal facts.
"""

from __future__ import annotations

import logging
import os

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MEMORY_ID = os.getenv("MEMORY_ID", "")
MEMORY_NAMESPACE = os.getenv("MEMORY_NAMESPACE", "")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")

_runtime_client = None


def _get_client():
    global _runtime_client
    if _runtime_client is None:
        _runtime_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    return _runtime_client


class _RecallInput(BaseModel):
    query: str = Field(
        description=(
            "Natural-language description of what to recall (e.g. "
            '"what did the user say about their team\'s sprint cadence?").'
        )
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of memory records to return (1–20).",
    )


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return str(content)
    return str(content)


def _recall(query: str, top_k: int = 5) -> str:
    if not MEMORY_ID or not MEMORY_NAMESPACE:
        return "Persistent memory is not configured for this agent."
    try:
        resp = _get_client().retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=MEMORY_NAMESPACE,
            searchCriteria={"searchQuery": query, "topK": top_k},
        )
        summaries = resp.get("memoryRecordSummaries") or []
        if not summaries:
            return "No relevant memories found from past sessions."
        lines = []
        for i, r in enumerate(summaries, 1):
            text = _extract_text(r.get("content"))
            score = r.get("score")
            head = f"[{i}]"
            if isinstance(score, (int, float)):
                head += f" (score={score:.2f})"
            lines.append(f"{head} {text}".strip())
        return "\n\n".join(lines)
    except Exception as e:
        logger.error("Memory recall failed: %s", e)
        return f"Memory recall failed: {e}"


def build_memory_tool() -> StructuredTool | None:
    if not MEMORY_ID:
        logger.info("Memory not provisioned — MEMORY_ID unset")
        return None
    return StructuredTool.from_function(
        func=_recall,
        name="recall_memory",
        description=(
            "Search the agent's long-term memory for facts extracted from "
            "previous conversations. Use this whenever the user references "
            "something that may have come up in an earlier chat — their own "
            "or another user's — instead of asking them to repeat themselves. "
            "Returns nothing on a cold start (no prior chats yet)."
        ),
        args_schema=_RecallInput,
    )
