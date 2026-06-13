"""AgentCore Browser tool — open URLs and extract text/HTML."""

from __future__ import annotations

import logging
import os
import threading

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BROWSER_RESOURCE_ID = os.getenv("BROWSER_RESOURCE_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")

_session_lock = threading.Lock()
_session_id: str | None = None


def _client():
    return boto3.client("bedrock-agentcore", region_name=AWS_REGION)


def _get_or_create_session() -> str:
    global _session_id
    with _session_lock:
        if _session_id is None:
            resp = _client().start_browser_session(
                browserIdentifier=BROWSER_RESOURCE_ID,
                name="agent-browser",
                sessionTimeoutSeconds=3600,
            )
            _session_id = resp["sessionId"]
            logger.info("Started browser session: %s", _session_id)
        return _session_id


class _BrowseInput(BaseModel):
    url: str = Field(description="Fully-qualified URL to navigate to.")
    action: str = Field(
        default="get_text",
        description="'get_text' returns visible page text (default), 'get_html' returns raw HTML.",
    )


def _run(url: str, action: str = "get_text") -> str:
    if not BROWSER_RESOURCE_ID:
        return "Browser tool is not provisioned for this agent."
    try:
        session_id = _get_or_create_session()
        response = _client().invoke_browser(
            browserIdentifier=BROWSER_RESOURCE_ID,
            sessionId=session_id,
            name="navigate",
            arguments={"url": url, "extract": action},
        )
        parts: list[str] = []
        for event in response.get("stream") or []:
            for exc_key in (
                "accessDeniedException",
                "validationException",
                "internalServerException",
                "throttlingException",
                "resourceNotFoundException",
            ):
                if exc_key in event:
                    return f"[AgentCore error] {event[exc_key].get('message', exc_key)}"
            result = event.get("result") or {}
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "").rstrip()
                    if text:
                        parts.append(text)
        return "\n".join(parts) if parts else "(no content)"
    except Exception as e:
        logger.exception("Browser tool call failed")
        return f"[Browser error] {e}"


def build_browser_tool() -> StructuredTool | None:
    if not BROWSER_RESOURCE_ID:
        logger.info("Browser not provisioned — BROWSER_RESOURCE_ID unset")
        return None
    return StructuredTool.from_function(
        func=_run,
        name="browse",
        description=(
            "Open a web page in a sandboxed browser and return its text (or HTML). "
            "Use for live web content the agent needs to read."
        ),
        args_schema=_BrowseInput,
    )
