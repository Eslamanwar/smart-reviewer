"""Stage user-uploaded files into this agent's code-interpreter sandbox.

The BFF stores attachments in S3 under ``chat-uploads/...`` and ships the
S3 records in ``forwardedProps.attachments``. We fetch each file here and
write it into the sandbox at ``/uploads/<name>`` so the agent can open it
with plain ``open()`` from ``execute_code``.

- Text files (utf-8 decodable, <= 256 KiB) → writeFiles (sandbox API).
- Binary or large text → executeCode with an embedded base64 blob and
  Python decode. Capped at 5 MiB per file to keep the code payload sane.
- No-CI agents or oversized files: the file is dropped and a status note
  is returned so the system prompt can warn the user.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import boto3

from src.tools.code_interpreter import execute_in_sandbox

logger = logging.getLogger(__name__)

CODE_INTERPRETER_ID = os.getenv("CODE_INTERPRETER_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")

# Hard caps. writeFiles takes inline text; executeCode payload is bounded by
# AgentCore's invoke limits + practical SSE payload sanity.
_TEXT_LIMIT = 256 * 1024
_BINARY_LIMIT = 5 * 1024 * 1024


def _s3():
    return boto3.client("s3", region_name=AWS_REGION)


def _write_text(path: str, text: str) -> str:
    from src.tools.code_interpreter import _client, _get_or_create_session

    session_id = _get_or_create_session()
    resp = _client().invoke_code_interpreter(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID,
        sessionId=session_id,
        name="writeFiles",
        arguments={"content": [{"path": path, "text": text}]},
    )
    # Drain the stream so any error surfaces.
    for event in resp.get("stream", []):
        for exc_key in (
            "accessDeniedException",
            "validationException",
            "internalServerException",
            "throttlingException",
            "resourceNotFoundException",
        ):
            if exc_key in event:
                return f"sandbox error: {event[exc_key].get('message', exc_key)}"
    return "ok"


def _write_binary(path: str, data: bytes) -> str:
    """Base64-encode and decode-in-sandbox. AgentCore writeFiles is text-only."""
    b64 = base64.b64encode(data).decode("ascii")
    code = (
        "import base64, os\n"
        f"os.makedirs(os.path.dirname({path!r}), exist_ok=True)\n"
        f"with open({path!r}, 'wb') as fh:\n"
        f"    fh.write(base64.b64decode({b64!r}))\n"
        f"print('wrote', {path!r})"
    )
    out = execute_in_sandbox(code)
    return "ok" if out.startswith("wrote ") else f"sandbox error: {out[:200]}"


def stage_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Materialise each attachment in the sandbox; return per-file status."""
    if not CODE_INTERPRETER_ID:
        return [{**a, "sandbox_path": None, "status": "no sandbox on this agent"} for a in attachments]

    out: list[dict[str, Any]] = []
    s3 = _s3()
    # Make /uploads/ once — cheap, idempotent.
    try:
        execute_in_sandbox("import os; os.makedirs('/uploads', exist_ok=True)")
    except Exception as e:
        logger.warning("Could not create /uploads: %s", e)

    for a in attachments:
        bucket = a.get("bucket")
        key = a.get("key")
        name = a.get("name") or "file"
        mime = a.get("mime") or "application/octet-stream"
        sandbox_path = f"/uploads/{name}"
        record: dict[str, Any] = {"name": name, "mime": mime, "sandbox_path": sandbox_path}
        if not bucket or not key:
            record["status"] = "missing bucket/key"
            out.append(record)
            continue
        try:
            resp = s3.get_object(Bucket=bucket, Key=key)
            data = resp["Body"].read()
            record["size"] = len(data)
        except Exception as e:
            logger.warning("S3 fetch failed for %s/%s: %s", bucket, key, e)
            record["status"] = f"S3 fetch failed: {e}"
            out.append(record)
            continue

        # Try text path first for small files; fall back to binary.
        wrote = None
        if len(data) <= _TEXT_LIMIT:
            try:
                text = data.decode("utf-8")
                wrote = _write_text(sandbox_path, text)
            except UnicodeDecodeError:
                wrote = None

        if wrote is None:
            if len(data) > _BINARY_LIMIT:
                record["status"] = f"too large to stage ({len(data)} bytes > {_BINARY_LIMIT})"
                out.append(record)
                continue
            wrote = _write_binary(sandbox_path, data)

        record["status"] = wrote
        out.append(record)

    return out
