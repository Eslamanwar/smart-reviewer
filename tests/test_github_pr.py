"""Unit tests for the GitHub PR fetch tool — pure parsing/formatting, no network.

Network calls are stubbed via monkeypatching ``_request`` so these run
offline and deterministically.
"""

import json

import pytest
from src.tools.github_pr import _fetch_pr, _parse_pr, build_github_pr_tool


class TestParsePR:
    def test_full_url(self):
        assert _parse_pr("https://github.com/langchain-ai/deepagents/pull/3936", None, None) == (
            "langchain-ai",
            "deepagents",
            3936,
        )

    def test_url_with_trailing_path(self):
        assert _parse_pr("https://github.com/o/r/pull/12/files#diff-abc", None, None) == ("o", "r", 12)

    def test_short_form(self):
        assert _parse_pr("langchain-ai/deepagents#3936", None, None) == ("langchain-ai", "deepagents", 3936)

    def test_bare_number_with_owner_repo(self):
        assert _parse_pr("3936", "langchain-ai", "deepagents") == ("langchain-ai", "deepagents", 3936)

    def test_bare_number_without_owner_repo_raises(self):
        with pytest.raises(ValueError):
            _parse_pr("3936", None, None)

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            _parse_pr("not a pr", None, None)


class TestFetchPR:
    def _stub(self, monkeypatch, *, meta_status=200, meta=None, files=None, diff="diff --git a b\n+x\n"):
        meta = meta or {
            "title": "Fix the thing",
            "user": {"login": "alice"},
            "state": "open",
            "merged": False,
            "draft": False,
            "head": {"label": "alice:fix"},
            "base": {"label": "main"},
            "additions": 10,
            "deletions": 2,
            "changed_files": 1,
            "commits": 1,
            "html_url": "https://github.com/o/r/pull/1",
            "body": "Closes #99",
        }
        files = (
            files
            if files is not None
            else [{"status": "modified", "additions": 10, "deletions": 2, "filename": "a.py"}]
        )

        def fake_request(url, accept):
            if accept == "application/vnd.github.v3.diff":
                return 200, diff
            if url.endswith("/files?per_page=300"):
                return 200, json.dumps(files)
            return meta_status, json.dumps(meta)

        monkeypatch.setattr("src.tools.github_pr._request", fake_request)

    def test_happy_path(self, monkeypatch):
        self._stub(monkeypatch)
        out = _fetch_pr("o/r#1")
        assert "PR #1: Fix the thing" in out
        assert "Author: @alice" in out
        assert "+10 / -2" in out
        assert "## Changed files" in out
        assert "a.py" in out
        assert "## Unified diff" in out
        assert "diff --git" in out

    def test_metadata_404(self, monkeypatch):
        self._stub(monkeypatch, meta_status=404, meta={"message": "Not Found"})
        out = _fetch_pr("o/r#1")
        assert out.startswith("ERROR:")
        assert "404" in out

    def test_include_diff_false_skips_diff(self, monkeypatch):
        self._stub(monkeypatch)
        out = _fetch_pr("o/r#1", include_diff=False)
        assert "## Unified diff" not in out
        assert "## Changed files" in out

    def test_diff_truncation_flagged(self, monkeypatch):
        from src.tools import github_pr

        big = "x" * (github_pr.MAX_DIFF_CHARS + 100)
        self._stub(monkeypatch, diff=big)
        out = _fetch_pr("o/r#1")
        assert "truncated" in out.lower()

    def test_bad_reference_returns_error_not_raise(self, monkeypatch):
        self._stub(monkeypatch)
        out = _fetch_pr("totally bogus")
        assert out.startswith("ERROR:")


def test_build_tool_exposes_name():
    tool = build_github_pr_tool()
    assert tool is not None
    assert tool.name == "fetch_github_pr"
