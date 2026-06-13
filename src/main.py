"""Auto-generated agent entrypoint — FastAPI + AG-UI.

Per-request: looks up the user's email + the live MCP catalog from
``forwardedProps`` (injected by the agents-hub-app BFF, validated against
the Cloudflare JWT), then builds a per-user deepagent with its MCP tools
wired up. Per-user agents are cached in memory.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import uvicorn
from ag_ui.core import CustomEvent, EventType, RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

logging.basicConfig(stream=sys.stdout, level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

app = FastAPI(title="smart-code-reviewer", version="0.1.0")

_ready = asyncio.Event()

# Static single-page UI for streaming reviews (served same-origin as
# /invocations, so no CORS config is needed). See static/index.html.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/ping")
async def ping():
    return JSONResponse({"status": "healthy", "agent": "smart-code-reviewer-y7b4ip"})


@app.get("/ui")
async def ui():
    """Minimal browser UI that POSTs to /invocations and renders the SSE stream."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/invocations/health")
async def invocations_health():
    return {"status": "ok", "agent": {"name": "smart-code-reviewer-y7b4ip"}}


@app.post("/invocations")
async def invocations(input_data: RunAgentInput, request: Request):
    await _ready.wait()
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)

    props: dict[str, Any] = input_data.forwarded_props or {}
    available_mcps = props.get("available_mcps") or []
    user_email = props.get("user_email")
    mcp_tokens = props.get("mcp_tokens") or {}
    context_notes = props.get("context_notes") or ""
    attachments = props.get("attachments") or []

    from ag_ui_langgraph import LangGraphAgent
    from langgraph.checkpoint.memory import MemorySaver

    import src.agent.litellm_patch  # noqa: F401
    from src.agent.factory import create_agent

    # Stage user-uploaded files into the CI sandbox at /uploads/<name>.
    # No-op when the agent has no code interpreter or when no files were sent.
    attachments_info: list[dict[str, Any]] = []
    if attachments:
        try:
            from src.attachments import stage_attachments

            attachments_info = stage_attachments(attachments)
        except Exception as e:
            logger.warning("Failed to stage attachments: %s", e)

    graph = await create_agent(
        user_email=user_email,
        available_mcps=available_mcps,
        mcp_tokens=mcp_tokens,
        context_notes=context_notes,
        attachments_info=attachments_info,
    )
    if graph.checkpointer is None:
        graph.checkpointer = MemorySaver()

    # Last user message used as the LangSmith trace name so the trace list
    # shows the actual question instead of the user's email. user_email
    # moves into metadata where it belongs (filterable, not shown as input).
    last_user_msg = ""
    for m in reversed(input_data.messages or []):
        if getattr(m, "role", None) == "user":
            content = getattr(m, "content", "")
            last_user_msg = content if isinstance(content, str) else str(content)
            break
    run_name = (last_user_msg.strip().splitlines()[0][:80] if last_user_msg else "chat") or "chat"

    request_agent = LangGraphAgent(
        name="smart-code-reviewer-y7b4ip",
        graph=graph,
        config={
            "recursion_limit": 100,
            "metadata": {"user_email": user_email or "anon"},
            "tags": [user_email] if user_email else [],
            "run_name": run_name,
        },
    )

    async def event_generator():
        # The frontend uses the LangSmith run_id as the feedback message_id
        # so backend feedback writes can attach to the actual trace. Watch
        # the first root RAW event (parent_ids == []) and emit a custom
        # LANGSMITH_TRACE event the frontend already knows how to consume.
        langsmith_trace_emitted = False
        async for event in request_agent.run(input_data):
            if not langsmith_trace_emitted and event.type == EventType.RAW:
                raw_evt = getattr(event, "event", None) or {}
                raw_run_id = raw_evt.get("run_id", "") if isinstance(raw_evt, dict) else ""
                if raw_run_id and raw_evt.get("parent_ids") == []:
                    langsmith_trace_emitted = True
                    yield encoder.encode(
                        CustomEvent(
                            name="LANGSMITH_TRACE",
                            value={"runId": raw_run_id},
                        )
                    )
            yield encoder.encode(event)

    return StreamingResponse(event_generator(), media_type=encoder.get_content_type())


@app.on_event("startup")
async def startup():
    asyncio.create_task(_warmup())


async def _warmup():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _import_heavy)
    _ready.set()
    logger.info("Agent ready")


def _import_heavy():
    import src.agent.factory  # noqa: F401
    import src.agent.litellm_patch  # noqa: F401
    import src.tools.mcp  # noqa: F401


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
