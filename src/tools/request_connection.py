"""Surface an "MCP not connected" prompt as a clickable Connect card.

Called when the agent realises an MCP it needs hasn't been authorised by
the current user yet (the load report at session start, ``render_mcp_status_note``,
already lists which MCPs failed). Instead of telling the user "go to
settings", emit a structured marker the frontend recognises and renders as
a Connect button right inside the chat — one click opens the same OAuth
popup the MCP picker uses.
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _ConnectInput(BaseModel):
    slug: str = Field(
        description=(
            "The MCP server slug to connect, exactly as it appears in the "
            "agent's configured MCP list (the same slugs render_mcp_status_note "
            "showed at the start of the session). Never invent slugs."
        )
    )
    reason: str = Field(
        default="",
        description=(
            "One short sentence explaining why this connection is needed for "
            "the user's current request. Shown under the Connect button so "
            "the user understands what they're authorising."
        ),
    )


def _request_connection(slug: str, reason: str = "") -> str:
    payload = {"_kind": "auth_connect", "slug": slug, "reason": (reason or "").strip()}
    return "<<AUTH_CONNECT>>" + json.dumps(payload)


def build_request_connection_tool(mcp_slugs: list[str]) -> StructuredTool | None:
    """Build the tool only when the agent actually has MCPs to connect to.

    No MCPs configured → no point exposing it.
    """
    if not mcp_slugs:
        return None
    return StructuredTool.from_function(
        func=_request_connection,
        name="request_connection",
        description=(
            "Surface a one-click Connect button for an MCP server the user "
            "hasn't authorised yet. Use this when your previous tool call "
            "failed with an authorization error, OR when the session-start "
            "MCP status note showed a server you need is not connected. "
            "DO NOT use this for MCPs that already loaded successfully — "
            "the user has nothing to do. After calling this, briefly tell "
            "the user what's happening (one sentence); the Connect card "
            "appears inline in your message."
        ),
        args_schema=_ConnectInput,
    )
