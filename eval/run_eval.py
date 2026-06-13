#!/usr/bin/env python
"""LLM-as-judge eval for the smart-code-reviewer agent.

For each PR in ``dataset.yml`` it:
  1. runs THIS agent on the PR (in-process — no HTTP server needed),
  2. fetches the real HUMAN review (review summaries + inline comments) from the
     GitHub API and treats it as ground truth,
  3. asks an LLM judge how well the agent's review covers what the humans caught
     (recall) plus the agent's standalone quality,
  4. writes a per-PR scorecard (JSON + Markdown) to ``eval/results/`` and prints
     a summary table.

Usage (from the repo root):
  uv run python eval/run_eval.py                 # full run over the dataset
  uv run python eval/run_eval.py --dry-run       # just fetch+print human reviews (no LLM, no key)
  uv run python eval/run_eval.py --pr OWNER/REPO#N   # one ad-hoc PR
  uv run python eval/run_eval.py --limit 1       # first N dataset entries

Env:
  OPENROUTER_API_KEY / LLM_API_KEY   required for the agent + judge (loaded from .env)
  JUDGE_MODEL                        judge model (default: google/gemini-2.5-pro)
  GITHUB_TOKEN                       optional; raises GitHub API rate limit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the repo root importable no matter where this is run from, and trigger
# src/__init__ which loads .env.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.config import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
from src.tools.github_pr import _parse_pr, _request  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "google/gemini-2.5-pro")

# Reviewer logins that are bots, not humans (marked in the human-review blob).
_BOT_HINTS = ("[bot]", "copilot", "github-actions", "dependabot", "codecov")


def _is_bot(login: str) -> bool:
    low = (login or "").lower()
    return any(h in low for h in _BOT_HINTS)


# ──────────────────────────────────────────────────────────────────────────
# 1. Human review (ground truth) from the GitHub API
# ──────────────────────────────────────────────────────────────────────────
def fetch_human_review(pr_ref: str) -> dict:
    """Return {owner, repo, number, author, text, n_findings_raw} for a PR.

    ``text`` is a formatted blob of review summaries + inline comments left by
    everyone except the PR author, with bots marked. Excludes the author's own
    replies so the judge sees reviewers' feedback, not the author defending it.
    """
    owner, repo, number = _parse_pr(pr_ref, None, None)
    base = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"

    status, body = _request(base, "application/vnd.github+json")
    if status != 200:
        raise RuntimeError(f"PR metadata fetch failed ({status}) for {pr_ref}")
    meta = json.loads(body)
    author = (meta.get("user") or {}).get("login", "")

    def _get(path: str) -> list:
        st, bd = _request(f"{base}{path}?per_page=100", "application/vnd.github+json")
        if st != 200:
            return []
        try:
            return json.loads(bd) or []
        except ValueError:
            return []

    reviews = _get("/reviews")
    comments = _get("/comments")  # inline review comments

    lines: list[str] = []
    raw_count = 0

    for rv in reviews:
        login = (rv.get("user") or {}).get("login", "?")
        bd = (rv.get("body") or "").strip()
        if not bd or login == author:
            continue
        tag = " [bot]" if _is_bot(login) else ""
        lines.append(f"## Review by @{login}{tag} — {rv.get('state', '')}\n{bd}")
        raw_count += 1

    if comments:
        lines.append("## Inline comments")
        for c in comments:
            login = (c.get("user") or {}).get("login", "?")
            if login == author:
                continue
            path = c.get("path", "?")
            line_no = c.get("line") or c.get("original_line") or "?"
            tag = " [bot]" if _is_bot(login) else ""
            bd = (c.get("body") or "").strip().replace("\n", " ")
            lines.append(f"- [{path}:{line_no}] @{login}{tag}: {bd}")
            raw_count += 1

    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "author": author,
        "text": "\n\n".join(lines) if lines else "(no human review comments found)",
        "n_findings_raw": raw_count,
    }


# ──────────────────────────────────────────────────────────────────────────
# 2. Agent under test (in-process)
# ──────────────────────────────────────────────────────────────────────────
async def run_agent_review(pr_ref: str) -> str:
    """Build the agent and run a single review; return the final review markdown."""
    from langchain_core.messages import HumanMessage

    import src.agent.litellm_patch  # noqa: F401
    from src.agent.factory import create_agent

    graph = await create_agent(user_email="eval@local")
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=f"Review {pr_ref}")]},
        config={"recursion_limit": 100},
    )
    for m in reversed(result.get("messages", [])):
        if getattr(m, "type", None) != "ai":
            continue
        content = _text_from_content(getattr(m, "content", ""))
        if content.strip():
            return content
    return "(agent produced no review text)"


# ──────────────────────────────────────────────────────────────────────────
# 3. The judge
# ──────────────────────────────────────────────────────────────────────────
def _text_from_content(content) -> str:
    """Flatten a LangChain message ``content`` to plain text.

    Reasoning models (e.g. gemini-2.5-pro) return a list of blocks like
    ``[{"type": "thinking", ...}, {"type": "text", "text": "..."}]``; we keep
    only the text blocks. Plain string content is returned as-is.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        )
    return str(content)


def _parse_json_blob(text: str) -> dict:
    """Best-effort: pull the first {...} JSON object out of the judge's reply."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def run_judge(diff_blob: str, agent_review: str, human_review: str) -> dict:
    """Call the judge model and return the parsed scorecard dict."""
    import src.agent.litellm_patch  # noqa: F401
    from langchain_litellm import ChatLiteLLM

    rubric = (EVAL_DIR / "judge_prompt.md").read_text()
    prompt = (
        f"{rubric}\n\n"
        f"=== PR METADATA + DIFF ===\n{diff_blob}\n\n"
        f"=== AGENT REVIEW (under test) ===\n{agent_review}\n\n"
        f"=== HUMAN REVIEW (ground truth) ===\n{human_review}\n\n"
        "Now output the JSON scorecard."
    )
    judge = ChatLiteLLM(
        model=JUDGE_MODEL,
        api_base=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        custom_llm_provider="openai",
        temperature=0,
    )
    resp = judge.invoke(prompt)
    content = _text_from_content(resp.content)
    try:
        return _parse_json_blob(content)
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"could not parse judge output: {e}", "raw": content[:4000]}


# ──────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────
def _slug(pr_ref: str) -> str:
    try:
        o, r, n = _parse_pr(pr_ref, None, None)
        return f"{o}__{r}__{n}"
    except ValueError:
        return re.sub(r"[^a-zA-Z0-9]+", "_", pr_ref).strip("_")


async def evaluate_pr(pr_ref: str, dry_run: bool) -> dict:
    print(f"\n=== {pr_ref} ===")
    print("  · fetching human review …")
    human = fetch_human_review(pr_ref)
    print(f"    {human['n_findings_raw']} reviewer comment(s) by non-authors")

    if dry_run:
        print("  · dry run — human review follows:\n")
        print(human["text"])
        return {"pr": pr_ref, "human": human, "dry_run": True}

    from src.tools.github_pr import _fetch_pr

    print("  · running the agent (this calls the model and may take a while) …")
    agent_review = await run_agent_review(pr_ref)
    print(f"    agent review: {len(agent_review)} chars")

    print("  · fetching diff for the judge …")
    diff_blob = _fetch_pr(pr_ref)

    print(f"  · judging with {JUDGE_MODEL} …")
    scorecard = run_judge(diff_blob, agent_review, human["text"])

    return {
        "pr": pr_ref,
        "judge_model": JUDGE_MODEL,
        "human_comment_count": human["n_findings_raw"],
        "agent_review": agent_review,
        "human_review": human["text"],
        "scorecard": scorecard,
    }


def _save(result: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = RESULTS_DIR / f"{_slug(result['pr'])}__{stamp}"
    base.with_suffix(".json").write_text(json.dumps(result, indent=2))
    sc = result.get("scorecard", {})
    md = [f"# Eval: {result['pr']}", "", f"Judge: `{result.get('judge_model', '?')}`", ""]
    if "error" in sc:
        md += ["**Judge error:** " + sc["error"], "", "```", sc.get("raw", ""), "```"]
    else:
        rec, q = sc.get("recall", {}), sc.get("quality", {})
        md += [
            f"**Recall:** {rec.get('caught', '?')} caught / {rec.get('partial', '?')} partial / "
            f"{rec.get('missed', '?')} missed of {rec.get('total', '?')} "
            f"(score {rec.get('score', '?')})",
            f"**Quality:** correctness {q.get('correctness', '?')}, specificity {q.get('specificity', '?')}, "
            f"actionability {q.get('actionability', '?')}, overall {q.get('overall', '?')}",
            "",
            "**Verdict:** " + sc.get("verdict", ""),
            "",
            "## Human findings vs agent",
        ]
        for f in sc.get("human_findings", []):
            md.append(f"- **{f.get('agent_status', '?')}** — {f.get('summary', '')}  \n  ↳ {f.get('evidence', '')}")
        md += ["", "## Agent-only findings"]
        for f in sc.get("agent_only_findings", []):
            md.append(f"- **{f.get('assessment', '?')}** — {f.get('summary', '')} ({f.get('reason', '')})")
    base.with_suffix(".md").write_text("\n".join(md))
    return base


def _print_summary(results: list[dict]) -> None:
    print("\n" + "=" * 78)
    print(f"{'PR':<42} {'recall':>10} {'quality':>9}")
    print("-" * 78)
    for r in results:
        sc = r.get("scorecard", {})
        if "error" in sc:
            print(f"{r['pr'][:42]:<42} {'JUDGE ERR':>10} {'-':>9}")
            continue
        rec = sc.get("recall", {}) or {}
        q = sc.get("quality", {}) or {}
        score = rec.get("score")
        rstr = f"{rec.get('caught', '?')}/{rec.get('total', '?')}" + (f" ({score})" if score is not None else "")
        print(f"{r['pr'][:42]:<42} {rstr:>10} {str(q.get('overall', '-')):>9}")
    print("=" * 78)


async def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-as-judge eval for smart-code-reviewer")
    ap.add_argument("--dry-run", action="store_true", help="fetch+print human reviews only (no LLM, no key)")
    ap.add_argument("--pr", help="evaluate a single ad-hoc PR reference instead of the dataset")
    ap.add_argument("--limit", type=int, default=0, help="only the first N dataset entries")
    args = ap.parse_args()

    if args.pr:
        pr_refs = [args.pr]
    else:
        entries = yaml.safe_load((EVAL_DIR / "dataset.yml").read_text()) or []
        pr_refs = [e["pr"] for e in entries]
        if args.limit:
            pr_refs = pr_refs[: args.limit]

    if not args.dry_run and not LLM_API_KEY:
        print(
            "ERROR: no OPENROUTER_API_KEY / LLM_API_KEY set (put it in .env).\n"
            "       Use --dry-run to fetch human reviews without calling any model.",
            file=sys.stderr,
        )
        return 2

    results = []
    for ref in pr_refs:
        try:
            res = await evaluate_pr(ref, dry_run=args.dry_run)
        except Exception as e:  # one bad PR shouldn't kill the run
            print(f"  ! failed: {e}")
            res = {"pr": ref, "scorecard": {"error": str(e)}}
        results.append(res)
        if not args.dry_run:
            saved = _save(res)
            print(f"  · saved {saved.with_suffix('.md').name}")

    if not args.dry_run:
        _print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
