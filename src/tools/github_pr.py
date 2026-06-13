"""Fetch a GitHub pull request (metadata + unified diff) for code review.

The agent reviews real, open pull requests — typically OSS repos on
github.com. This tool wraps the GitHub REST API so the LLM can pull a PR's
metadata, the list of changed files, and the unified diff in one call,
without needing a code interpreter or browser.

Network access only; no auth required for public repos. If a
``GITHUB_TOKEN`` env var is present it is used to lift the unauthenticated
rate limit (60 req/h) and to read private repos the token can see — but the
tool never logs or echoes the token.

Diffs can be enormous. The unified diff is hard-capped (``MAX_DIFF_CHARS``)
so a single huge PR cannot blow up the model's context window; truncation is
flagged explicitly so the reviewer knows the diff is incomplete.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
HTTP_TIMEOUT = float(os.getenv("GITHUB_HTTP_TIMEOUT", "30"))

# Hard cap on the unified diff returned to the model. ~200k chars keeps a
# large PR readable without swamping the context window. Past this we
# truncate and say so, so the reviewer never assumes it saw the whole change.
MAX_DIFF_CHARS = 200_000
# Cap on the number of changed files we enumerate in the summary.
MAX_FILES_LISTED = 300

# https://github.com/{owner}/{repo}/pull/{number}  (also tolerates a
# trailing /files, #discussion, query strings, and an "owner/repo#123" form)
_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
_SHORT_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/#\s]+)#(?P<number>\d+)$")


class _PRInput(BaseModel):
    pr: str = Field(
        description=(
            "The pull request to review. Accepts a full URL "
            "(https://github.com/owner/repo/pull/123), the short form "
            "'owner/repo#123', or just '123' when owner/repo are given "
            "separately."
        )
    )
    owner: str | None = Field(
        default=None,
        description="Repo owner/org. Required only if `pr` is a bare PR number.",
    )
    repo: str | None = Field(
        default=None,
        description="Repo name. Required only if `pr` is a bare PR number.",
    )
    include_diff: bool = Field(
        default=True,
        description=(
            "Include the unified diff (capped at ~200k chars). Set False to "
            "fetch only metadata + the changed-file list for a very large PR."
        ),
    )


def _parse_pr(pr: str, owner: str | None, repo: str | None) -> tuple[str, str, int]:
    pr = (pr or "").strip()
    m = _PR_URL_RE.search(pr)
    if m:
        return m["owner"], m["repo"].removesuffix(".git"), int(m["number"])
    m = _SHORT_RE.match(pr)
    if m:
        return m["owner"], m["repo"], int(m["number"])
    if pr.isdigit():
        if not owner or not repo:
            raise ValueError("A bare PR number requires both `owner` and `repo`.")
        return owner, repo, int(pr)
    raise ValueError(
        f"Could not parse pull request reference {pr!r}. Use a github.com PR "
        "URL, 'owner/repo#123', or a bare number with owner+repo."
    )


def _request(url: str, accept: str) -> tuple[int, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "smart-code-reviewer-y7b4ip",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (https only)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body


def _explain_error(status: int, body: str) -> str:
    detail = ""
    try:
        detail = (json.loads(body) or {}).get("message", "")
    except (ValueError, TypeError):
        detail = body[:200]
    if status == 404:
        return "GitHub returned 404 — the PR may not exist, or the repo is private and no GITHUB_TOKEN is set."
    if status == 403 and "rate limit" in detail.lower():
        return "GitHub rate limit hit. Set a GITHUB_TOKEN env var to raise the limit, then retry."
    return f"GitHub API error {status}: {detail or 'unknown error'}"


def _fetch_pr(
    pr: str,
    owner: str | None = None,
    repo: str | None = None,
    include_diff: bool = True,
) -> str:
    try:
        o, r, number = _parse_pr(pr, owner, repo)
    except ValueError as e:
        return f"ERROR: {e}"

    base = f"{GITHUB_API}/repos/{o}/{r}/pulls/{number}"

    status, body = _request(base, "application/vnd.github+json")
    if status != 200:
        return f"ERROR: {_explain_error(status, body)}"
    try:
        meta = json.loads(body)
    except ValueError:
        return "ERROR: GitHub returned a malformed metadata response."

    # Changed-file list (paginated; one page of up to 300 is plenty for a summary).
    files_status, files_body = _request(f"{base}/files?per_page={MAX_FILES_LISTED}", "application/vnd.github+json")
    files = []
    if files_status == 200:
        try:
            files = json.loads(files_body) or []
        except ValueError:
            files = []

    lines: list[str] = []
    head = meta.get("head") or {}
    bse = meta.get("base") or {}
    lines.append(f"# PR #{number}: {meta.get('title', '(no title)')}")
    lines.append(f"Repo: {o}/{r}")
    lines.append(f"Author: @{(meta.get('user') or {}).get('login', 'unknown')}")
    lines.append(
        f"State: {meta.get('state')}"
        + (" (merged)" if meta.get("merged") else "")
        + (" [DRAFT]" if meta.get("draft") else "")
    )
    lines.append(f"Base: {bse.get('label', '?')}  ←  Head: {head.get('label', '?')}")
    lines.append(
        f"Changes: +{meta.get('additions', '?')} / -{meta.get('deletions', '?')} "
        f"across {meta.get('changed_files', '?')} file(s), {meta.get('commits', '?')} commit(s)."
    )
    lines.append(f"URL: {meta.get('html_url', '')}")
    lines.append("")
    lines.append("## Description")
    lines.append((meta.get("body") or "(no description provided)").strip())
    lines.append("")

    if files:
        lines.append(f"## Changed files ({len(files)}{'+' if len(files) >= MAX_FILES_LISTED else ''})")
        for f in files:
            status = f.get("status", "?")
            churn = f"+{f.get('additions', 0)}/-{f.get('deletions', 0)}"
            lines.append(f"- {status:>8}  {churn}  {f.get('filename', '?')}")
        lines.append("")

    if include_diff:
        diff_status, diff_body = _request(base, "application/vnd.github.v3.diff")
        if diff_status != 200:
            lines.append(f"## Diff\n(could not fetch diff: {_explain_error(diff_status, diff_body)})")
        else:
            truncated = len(diff_body) > MAX_DIFF_CHARS
            shown = diff_body[:MAX_DIFF_CHARS]
            lines.append("## Unified diff")
            if truncated:
                lines.append(
                    f"> NOTE: diff truncated to {MAX_DIFF_CHARS} chars "
                    f"(full diff is {len(diff_body)} chars). Review what is shown and "
                    "say explicitly that the tail of the diff was not reviewed."
                )
            lines.append("```diff")
            lines.append(shown)
            lines.append("```")

    return "\n".join(lines)


def build_github_pr_tool() -> StructuredTool | None:
    return StructuredTool.from_function(
        func=_fetch_pr,
        name="fetch_github_pr",
        description=(
            "Fetch a GitHub pull request's metadata, changed-file list, and "
            "unified diff so you can review it. Works on public repos with no "
            "auth. Call this FIRST whenever the user gives you a PR URL, an "
            "'owner/repo#123' reference, or asks you to review a specific pull "
            "request. The diff is capped at ~200k chars; if it is truncated, "
            "review what you can and say the tail was not seen."
        ),
        args_schema=_PRInput,
    )
