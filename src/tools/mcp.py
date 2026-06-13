"""Materialise this agent's MCP slugs into LangChain tools.

The BFF injects ``user_email`` + ``available_mcps`` (server-validated)
into the AG-UI ``forwardedProps``. We use them to construct one
langchain-mcp-adapters HTTP client per slug, with ``X-User-Email`` set
so the gateway can resolve per-user OAuth tokens.

Per-server failures are non-fatal — we return a load report so the
factory can keep the agent honest about what's available right now vs
what was attempted but needs auth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)


@dataclass
class MCPFailure:
    reason: str
    auth_url: str | None = None


@dataclass
class MCPLoadResult:
    tools: list[BaseTool] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    not_in_catalog: list[str] = field(default_factory=list)
    failed: dict[str, MCPFailure] = field(default_factory=dict)


def _index_catalog(catalog: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {item["slug"]: item for item in (catalog or []) if item.get("slug")}


def _unwrap(exc: BaseException) -> BaseException:
    """Walk through ExceptionGroup / __cause__ wrappers to the underlying error."""
    seen: set[int] = set()
    cur: BaseException = exc
    while id(cur) not in seen:
        seen.add(id(cur))
        sub = getattr(cur, "exceptions", None)
        if sub:
            cur = sub[0]
            continue
        cause = cur.__cause__ or cur.__context__
        if cause and cause is not cur:
            cur = cause
            continue
        break
    return cur


def _format_error(exc: BaseException) -> str:
    inner = _unwrap(exc)
    response = getattr(inner, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        body = ""
        try:
            text = getattr(response, "text", None)
            if text:
                body = text.strip().splitlines()[0][:200]
        except Exception:
            pass
        if status:
            base = f"HTTP {status} — {type(inner).__name__}: {str(inner)[:120]}"
            return f"{base} | body: {body}" if body else base
    return f"{type(inner).__name__}: {str(inner)[:200]}"


async def build_mcp_tools(
    mcp_slugs: list[str],
    catalog: list[dict[str, Any]] | None,
    user_email: str | None,
    mcp_tokens: dict[str, dict[str, Any]] | None = None,
) -> MCPLoadResult:
    result = MCPLoadResult(attempted=list(mcp_slugs))
    if not mcp_slugs:
        return result

    by_slug = _index_catalog(catalog)
    tokens = mcp_tokens or {}

    server_config: dict[str, dict[str, Any]] = {}
    for slug in mcp_slugs:
        entry = by_slug.get(slug)
        if not entry or not entry.get("connection_url"):
            result.not_in_catalog.append(slug)
            continue
        headers: dict[str, str] = {}
        token_info = tokens.get(slug) or {}
        access_token = token_info.get("access_token")
        if access_token:
            token_type = token_info.get("token_type") or "Bearer"
            headers["Authorization"] = f"{token_type} {access_token}"
        elif user_email:
            headers["X-User-Email"] = user_email
        server_config[slug] = {
            "transport": "streamable_http",
            "url": entry["connection_url"],
            "headers": headers,
        }

    if result.not_in_catalog:
        logger.warning("Skipping MCPs not in catalog or missing URL: %s", result.not_in_catalog)
    if not server_config:
        return result

    for slug, cfg in server_config.items():
        auth_header = cfg.get("headers", {}).get("Authorization", "")
        auth_summary = (
            f"Bearer {auth_header.split(' ', 1)[1][:8]}…"
            if auth_header.lower().startswith("bearer ")
            else (auth_header[:16] + "…") if auth_header else "<no-auth>"
        )
        logger.info("Calling MCP %r at %s with %s", slug, cfg.get("url"), auth_summary)
        try:
            client = MultiServerMCPClient({slug: cfg})
            tools = await client.get_tools()
            result.tools.extend(tools)
            result.loaded.append(slug)
            logger.info("Loaded MCP %r → %d tools", slug, len(tools))
        except Exception as e:
            reason = _format_error(e)
            auth_url = by_slug.get(slug, {}).get("connection_url") or None
            result.failed[slug] = MCPFailure(reason=reason, auth_url=auth_url)
            logger.warning("Failed to load MCP %r: %s", slug, reason)
    return result


def render_mcp_status_note(result: MCPLoadResult) -> str:
    if not result.attempted:
        return ""
    parts = [
        "## Available MCP tools",
        f"Configured for this agent: {', '.join(result.attempted)}",
    ]
    if result.loaded:
        parts.append(f"✓ Connected (tools available): {', '.join(result.loaded)}")
    if result.failed:
        parts.append("✗ Failed to connect (needs user authorization):")
        for slug, failure in result.failed.items():
            parts.append(f"  - **{slug}** — {failure.reason}")
        parts.append(
            "If the user asks you to use one of the failed MCPs, **call the "
            "`request_connection` tool with that slug** so a one-click Connect "
            "button appears inline in your message. Do NOT paste URLs or tell "
            "the user to navigate elsewhere. Do not pretend the MCP isn't "
            "configured."
        )
    if result.not_in_catalog:
        parts.append(f"✗ Unknown slugs (not in MCP catalog): {', '.join(result.not_in_catalog)}")
    return "\n".join(parts)
