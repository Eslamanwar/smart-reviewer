"""Factory for the smart-code-reviewer deepagent.

Built per-user (per ``forwardedProps.user_email``) so the MCP tools include
the right per-user identity in the gateway request headers. Cached in
memory by user_email so each user pays the build cost only once.
"""

import logging
import threading
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_litellm import ChatLiteLLM

import src.agent.litellm_patch  # noqa: F401
from src.agent.prompts import (
    SPECIALISTS,
    VERIFIER,
    get_system_prompt,
    load_specialist_prompt,
)
from src.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL_NAME,
    LLM_REASONING_EFFORT,
)
from src.tools.browser import build_browser_tool
from src.tools.code_interpreter import build_code_interpreter_tool
from src.tools.github_pr import build_github_pr_tool
from src.tools.knowledge_base import build_knowledge_base_tool
from src.tools.mcp import build_mcp_tools, render_mcp_status_note
from src.tools.memory import build_memory_tool
from src.tools.request_connection import build_request_connection_tool
from src.tools.share_file import build_share_file_tool

logger = logging.getLogger(__name__)

# MCP slugs frozen at deploy time — the agent's blueprint snapshot.
MCP_SLUGS: list[str] = []
CODE_INTERPRETER_ENABLED = False
BROWSER_TOOL_ENABLED = False
KNOWLEDGE_BASE_ENABLED = False
MEMORY_ENABLED = True
# Core capability of this agent: pull a GitHub PR's diff so it can be reviewed.
GITHUB_PR_TOOL_ENABLED = True
# Skills are read from disk via a FilesystemBackend. In the default
# non-virtual mode, absolute paths are used as-is and relative paths
# resolve under ``root_dir``, so we pass the absolute filesystem path of
# the skills folder directly. (Without an explicit FilesystemBackend,
# deepagents defaults to an in-memory StateBackend that expects skill
# files via ``invoke(files=...)`` and finds nothing on disk.)
SKILLS_ROOT = "/app"
SKILLS_DIR = "/app/skills"

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()
_MAX_CACHE = 16


def _create_llm() -> BaseChatModel:
    # OpenRouter is OpenAI-compatible, so we route through LiteLLM's "openai"
    # provider pointed at LLM_BASE_URL. reasoning_effort is only sent for
    # reasoning models (LLM_REASONING_EFFORT set) — Gemini Flash and most cheap
    # models reject it. parallel_tool_calls is likewise OpenAI/Anthropic-only,
    # so it rides along under the same flag.
    model_kwargs: dict[str, Any] = {}
    if LLM_REASONING_EFFORT:
        model_kwargs["reasoning_effort"] = LLM_REASONING_EFFORT
        model_kwargs["parallel_tool_calls"] = True
        model_kwargs["allowed_openai_params"] = ["reasoning_effort"]
    return ChatLiteLLM(
        model=LLM_MODEL_NAME,
        api_base=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        custom_llm_provider="openai",
        streaming=True,
        temperature=1,
        model_kwargs=model_kwargs,
    )


def _build_subagents(pr_tool: Any, ci_tool: Any = None) -> list[dict[str, Any]]:
    """Specialist sub-reviewers the orchestrator delegates to via the `task` tool.

    The five static dimension specialists each get ONLY the github_pr fetch tool
    so they can pull the diff into their own isolated context — keeping the
    orchestrator lean and each review focused on a single dimension.

    When an execution sandbox is provisioned (``ci_tool`` is not None), a sixth
    ``verification-reviewer`` is added: it gets the sandbox's ``execute_code``
    tool (plus github_pr) so it can clone the repo and run the tests/linter to
    confirm findings with real output. Omitted entirely when there's no sandbox,
    so the orchestrator never offers a capability it can't fulfil.

    ``model`` is omitted so deepagents inherits the parent's model; deepagents
    also gives every subagent the shared filesystem + planning tools.
    """
    diff_tools = [pr_tool] if pr_tool is not None else []
    subagents = [
        {
            "name": name,
            "description": description,
            "system_prompt": load_specialist_prompt(prompt_file),
            "tools": diff_tools,
        }
        for name, description, prompt_file in SPECIALISTS
    ]
    if ci_tool is not None:
        v_name, v_desc, v_prompt = VERIFIER
        subagents.append(
            {
                "name": v_name,
                "description": v_desc,
                "system_prompt": load_specialist_prompt(v_prompt),
                "tools": [ci_tool, *diff_tools],
            }
        )
    return subagents


def _tokens_signature(mcp_tokens: dict[str, dict[str, Any]] | None) -> str:
    if not mcp_tokens:
        return ""
    return ",".join(sorted(slug for slug, info in mcp_tokens.items() if (info or {}).get("access_token")))


def _render_attachments_note(attachments_info: list[dict[str, Any]] | None) -> str:
    if not attachments_info:
        return ""
    lines = ["## Attachments", ""]
    lines.append(
        "The user attached file(s) to this turn. Files that staged "
        "successfully are available inside the sandbox at the paths below — "
        "use `execute_code` to open them (e.g. `pd.read_csv('/uploads/x.csv')`). "
        "Do NOT pretend a failed file is available."
    )
    lines.append("")
    for a in attachments_info:
        status = a.get("status") or "ok"
        path = a.get("sandbox_path")
        mime = a.get("mime") or ""
        size = a.get("size")
        size_str = f" ({size} bytes)" if isinstance(size, int) else ""
        if status == "ok" and path:
            lines.append(f"- `{path}` — {mime}{size_str}")
        else:
            lines.append(f"- `{a.get('name', 'file')}` — NOT staged: {status}")
    return "\n".join(lines)


async def create_agent(
    user_email: str | None = None,
    available_mcps: list[dict[str, Any]] | None = None,
    mcp_tokens: dict[str, dict[str, Any]] | None = None,
    context_notes: str | None = None,
    attachments_info: list[dict[str, Any]] | None = None,
):
    # Hash context_notes into the cache key so a notes edit (delivered via
    # forwardedProps on the next request) invalidates the cached agent
    # without requiring a redeploy. Attachments deliberately bypass the
    # cache (they're per-turn) — we rebuild the prompt below outside any
    # caching path.
    import hashlib

    notes_sig = hashlib.sha1((context_notes or "").encode("utf-8")).hexdigest()[:8] if context_notes else ""
    # Attachments change per turn — include their identity in the cache key
    # so a new upload triggers a rebuild with the fresh prompt section.
    att_sig = (
        hashlib.sha1(
            "|".join(f"{a.get('sandbox_path', '')}::{a.get('status', '')}" for a in (attachments_info or [])).encode(
                "utf-8"
            )
        ).hexdigest()[:8]
        if attachments_info
        else ""
    )
    key = f"{user_email or '__anon__'}::{_tokens_signature(mcp_tokens)}" f"::{notes_sig}::{att_sig}"
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    llm = _create_llm()
    base_prompt = get_system_prompt()
    load = await build_mcp_tools(MCP_SLUGS, available_mcps, user_email, mcp_tokens)
    note = render_mcp_status_note(load)
    parts = [base_prompt]
    if context_notes and context_notes.strip():
        parts.append(f"## Context Notes\n\n{context_notes.strip()}")
    att_note = _render_attachments_note(attachments_info)
    if att_note:
        parts.append(att_note)
    if note:
        parts.append(note)
    system_prompt = "\n\n".join(parts)

    tools = list(load.tools)
    # Build the sandbox tool once (None when CODE_INTERPRETER_ID is unset). It's
    # given to the verification-reviewer subagent below regardless of whether the
    # orchestrator itself exposes execute_code.
    ci_tool = build_code_interpreter_tool()
    if CODE_INTERPRETER_ENABLED and ci_tool is not None:
        tools.append(ci_tool)
        # share_file is only meaningful when the sandbox exists — it pulls
        # files written by ``execute_code`` out for the user to download.
        sf = build_share_file_tool()
        if sf is not None:
            tools.append(sf)
    if BROWSER_TOOL_ENABLED:
        bt = build_browser_tool()
        if bt is not None:
            tools.append(bt)
    if KNOWLEDGE_BASE_ENABLED:
        kb = build_knowledge_base_tool()
        if kb is not None:
            tools.append(kb)
    if MEMORY_ENABLED:
        mem = build_memory_tool()
        if mem is not None:
            tools.append(mem)
    pr_tool = None
    if GITHUB_PR_TOOL_ENABLED:
        pr_tool = build_github_pr_tool()
        if pr_tool is not None:
            tools.append(pr_tool)
    # request_connection: lets the LLM surface an inline "Connect <MCP>"
    # button when it realises an MCP it needs is not authorised. Only
    # makes sense when the agent actually has MCPs configured.
    rc = build_request_connection_tool(MCP_SLUGS)
    if rc is not None:
        tools.append(rc)

    # Specialist sub-reviewers. The orchestrator (system_prompt above) plans,
    # fetches PR metadata, then delegates each review dimension to one of these
    # via the auto-provided `task` tool, and synthesizes their findings. The
    # verification-reviewer is included only when a sandbox is provisioned.
    subagents = _build_subagents(pr_tool, ci_tool)

    agent = create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        subagents=subagents,
        name="smart-code-reviewer-y7b4ip",
    )

    with _cache_lock:
        if len(_cache) >= _MAX_CACHE:
            try:
                _cache.pop(next(iter(_cache)))
            except StopIteration:
                pass
        _cache[key] = agent

    logger.info(
        "Built smart-code-reviewer-y7b4ip | user=%s | configured=%d | loaded=%d | failed=%d | subagents=%d",
        user_email or "anon",
        len(load.attempted),
        len(load.loaded),
        len(load.failed),
        len(subagents),
    )
    return agent
