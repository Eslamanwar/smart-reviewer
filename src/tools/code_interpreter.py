"""AgentCore Code Interpreter — sandboxed Python runtime for this agent."""

from __future__ import annotations

import logging
import os
import threading

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CODE_INTERPRETER_ID = os.getenv("CODE_INTERPRETER_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")

_session_lock = threading.Lock()
_session_id: str | None = None


def _client():
    return boto3.client("bedrock-agentcore", region_name=AWS_REGION)


def _get_or_create_session() -> str:
    global _session_id
    with _session_lock:
        if _session_id is None:
            resp = _client().start_code_interpreter_session(
                codeInterpreterIdentifier=CODE_INTERPRETER_ID,
                name="agent-session",
                sessionTimeoutSeconds=3600,
            )
            _session_id = resp["sessionId"]
            logger.info("Started code interpreter session: %s", _session_id)
        return _session_id


class _CodeInput(BaseModel):
    code: str = Field(description="Python code to run in the sandbox. Use print() to emit results.")
    clear_context: bool = Field(default=False, description="If True, reset variables/imports first.")


def _run(code: str, clear_context: bool = False) -> str:
    if not CODE_INTERPRETER_ID:
        return "Code interpreter is not provisioned for this agent."
    try:
        session_id = _get_or_create_session()
        response = _client().invoke_code_interpreter(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID,
            sessionId=session_id,
            name="executeCode",
            arguments={
                "code": code,
                "language": "python",
                "clearContext": clear_context,
            },
        )
        parts: list[str] = []
        for event in response["stream"]:
            for exc_key in (
                "accessDeniedException",
                "validationException",
                "internalServerException",
                "throttlingException",
                "resourceNotFoundException",
            ):
                if exc_key in event:
                    return f"[AgentCore error] {event[exc_key].get('message', exc_key)}"
            result = event.get("result")
            if result is None:
                continue
            if result.get("isError"):
                for item in result.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(f"[Error] {item.get('text', '')}")
                continue
            sc = result.get("structuredContent") or {}
            if sc.get("stdout"):
                parts.append(sc["stdout"].rstrip())
            if sc.get("stderr"):
                parts.append(f"[stderr]\n{sc['stderr'].rstrip()}")
            if sc.get("exitCode") and sc["exitCode"] != 0:
                parts.append(f"[exit code: {sc['exitCode']}]")
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "").rstrip()
                    if text:
                        parts.append(text)
        return "\n".join(parts) if parts else "(no output)"
    except Exception as e:
        logger.exception("Code interpreter call failed")
        return f"[Code interpreter error] {e}"


def execute_in_sandbox(code: str, clear_context: bool = False) -> str:
    """Public helper for sibling tools (e.g. share_file) that need to run
    code inside this agent's shared CI session. Returns concatenated stdout.
    """
    return _run(code, clear_context=clear_context)


def build_code_interpreter_tool() -> StructuredTool | None:
    if not CODE_INTERPRETER_ID:
        logger.info("Code interpreter not provisioned — CODE_INTERPRETER_ID unset")
        return None
    return StructuredTool.from_function(
        func=_run,
        name="execute_code",
        description=(
            "Run Python in a sandboxed AgentCore runtime. State persists across calls " "unless clear_context=True."
        ),
        args_schema=_CodeInput,
    )
