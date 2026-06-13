"""Surface a sandbox file as a downloadable chip in the chat UI.

The tool runs inside the SAME code-interpreter session as ``execute_code``
(via the public ``execute_in_sandbox`` helper), so files the agent has
written there are visible by their normal sandbox-relative path. The chip
is delivered as a single line of stdout with the prefix ``<<FILE_CHIP>>``
plus a JSON payload — the frontend recognises the marker, decodes the
base64, and renders a Download button.

A 10 MiB hard cap protects the SSE stream from blowing up. For larger
artifacts, write a smaller derived file (CSV slice, image thumbnail) and
share that instead.
"""

from __future__ import annotations

import logging
import textwrap

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.tools.code_interpreter import execute_in_sandbox

logger = logging.getLogger(__name__)


class _ShareInput(BaseModel):
    path: str = Field(
        description=(
            "Sandbox-relative path of the file to share, e.g. "
            "``report.csv`` or ``charts/sales_2026.png``. The file must "
            "already exist in the same sandbox session where ``execute_code`` "
            "wrote it."
        )
    )
    display_name: str | None = Field(
        default=None,
        description=("Optional friendly filename shown to the user (defaults to the " "file's basename)."),
    )


def _share(path: str, display_name: str | None = None) -> str:
    code = textwrap.dedent(
        f"""
        import base64, json, mimetypes, os
        p = {path!r}
        out = {{"_kind": "file_chip"}}
        if not os.path.exists(p):
            out["error"] = "not found"
            out["path"] = p
        elif not os.path.isfile(p):
            out["error"] = "not a file"
            out["path"] = p
        else:
            size = os.path.getsize(p)
            if size > 10 * 1024 * 1024:
                out["error"] = "too large (max 10 MiB)"
                out["size"] = size
                out["path"] = p
            else:
                with open(p, "rb") as fh:
                    data = base64.b64encode(fh.read()).decode("ascii")
                mime, _ = mimetypes.guess_type(p)
                out["name"] = {display_name!r} or os.path.basename(p)
                out["mime"] = mime or "application/octet-stream"
                out["size"] = size
                out["data_b64"] = data
        print("<<FILE_CHIP>>" + json.dumps(out))
        """
    ).strip()
    stdout = execute_in_sandbox(code)
    if "<<FILE_CHIP>>" not in stdout:
        return f"Could not share file (no chip emitted): {stdout}"
    return stdout


def build_share_file_tool() -> StructuredTool | None:
    return StructuredTool.from_function(
        func=_share,
        name="share_file",
        description=(
            "Make a file from the code-interpreter sandbox downloadable to "
            "the user. Call this AFTER writing the file with ``execute_code`` "
            "(or any other sandbox tool). The user sees a download chip in "
            "the chat — they get the actual file, not a base64 dump. Use "
            "this whenever the user asks for a report, export, generated "
            "image, chart, or any other artifact they want to keep. Hard "
            "cap is 10 MiB per file."
        ),
        args_schema=_ShareInput,
    )
