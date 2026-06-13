"""Patches for langchain-litellm 0.6.4 against the LiteLLM proxy + Anthropic
on Vertex. Three workarounds, all triggered by long tool-calling chats:

1. Strip ``thinking`` / ``redacted_thinking`` content blocks before sending —
   langchain-litellm 0.6.4 serializes them in a shape the proxy can't parse
   and we don't need them in history for Claude on Vertex anyway.

2. Flatten single-string content lists (``["x"]`` → ``"x"``) — Anthropic on
   Vertex 500s with "string indices must be integers" on the list-of-string
   shape that langchain produces after step 1.

3. Inject a dummy ``tools=[]`` parameter when the request has ``tool_calls``
   in history but no ``tools=`` declared. deepagents middlewares (e.g.
   SummarizationMiddleware) sometimes call the LLM without re-binding tools,
   and Anthropic on Vertex rejects that combination with a confusing 500.
   The dummy tool is a no-op placeholder the model is never expected to call.
"""

import langchain_litellm.chat_models.litellm as _litellm_mod
import litellm

# ── 1 + 2. Convert messages: strip thinking blocks, flatten ["x"] → "x" ──

_orig_convert = _litellm_mod._convert_message_to_dict
_THINKING = frozenset({"thinking", "redacted_thinking"})


def _convert_message(message):
    result = _orig_convert(message)
    content = result.get("content")
    if isinstance(content, list):
        filtered = [b for b in content if not (isinstance(b, dict) and b.get("type") in _THINKING)]
        # A single-string list (``["foo"]``) is what langchain produces when
        # the assistant response was just text. Anthropic on Vertex 500s on
        # that shape; unwrap to the plain string. Typed-dict lists stay as
        # lists.
        if len(filtered) == 1 and isinstance(filtered[0], str):
            result["content"] = filtered[0]
        else:
            result["content"] = filtered or ""
    return result


_litellm_mod._convert_message_to_dict = _convert_message

# ── 3. Inject dummy tool when history has tool_calls but tools= is missing ──

_orig_acompletion = litellm.acompletion
_DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "_history_placeholder_tool",
        "description": "Internal placeholder. Do not call.",
        "parameters": {"type": "object", "properties": {}},
    },
}


async def _patched_acompletion(*args, **kwargs):
    messages = kwargs.get("messages") or []
    needs_tools = any(isinstance(m, dict) and (m.get("tool_calls") or m.get("role") == "tool") for m in messages)
    if needs_tools and not kwargs.get("tools"):
        kwargs["tools"] = [_DUMMY_TOOL]
    return await _orig_acompletion(*args, **kwargs)


litellm.acompletion = _patched_acompletion
