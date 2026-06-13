"""System-prompt loader for this agent.

The reviewer is a small multi-agent system: a lean **orchestrator** that plans
and delegates, plus focused **specialist** sub-reviewers (one per review
dimension). The orchestrator prompt lives in ``{SLUG}.md``; each specialist
prompt lives under ``specialists/``.
"""

from datetime import date
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
SPECIALISTS_DIR = PROMPTS_DIR / "specialists"
SLUG = "smart-code-reviewer-y7b4ip"

# (subagent name, one-line description the orchestrator sees when routing,
# specialist prompt filename under specialists/). The description is what the
# `task` tool surfaces, so the orchestrator decides delegation from it.
SPECIALISTS: list[tuple[str, str, str]] = [
    (
        "correctness-reviewer",
        "Finds logic bugs, edge-case and boundary failures, error-handling gaps, "
        "concurrency/race conditions, and data-integrity risks in the PR diff.",
        "correctness",
    ),
    (
        "security-reviewer",
        "Audits the PR diff for security issues: injection, hardcoded secrets, "
        "auth/authz gaps, unsafe deserialization, sensitive data in logs (OWASP).",
        "security",
    ),
    (
        "performance-reviewer",
        "Reviews the PR diff for performance issues: algorithmic complexity, N+1 "
        "queries, missing caching, hot-path allocations, chatty I/O.",
        "performance",
    ),
    (
        "maintainability-reviewer",
        "Reviews readability, naming, structure, SOLID/DRY, coupling/cohesion, and "
        "architecture/design of the PR diff.",
        "maintainability",
    ),
    (
        "testing-reviewer",
        "Assesses test coverage and observability of the PR diff: missing "
        "unit/integration/edge/error-path tests, logging, metrics, tracing.",
        "testing",
    ),
]

# The verification specialist is added only when an execution sandbox is
# provisioned (see factory). It clones the repo and runs the tests/linter to
# turn suspected findings into confirmed facts — so it's gated on the sandbox
# rather than living in the always-on SPECIALISTS list above.
VERIFIER: tuple[str, str, str] = (
    "verification-reviewer",
    "Dynamically VERIFIES the change: clones the repo at the PR head in a "
    "sandbox, runs the tests and linter, and confirms/refutes specific findings "
    "with real command output. Dispatch to ground high-impact findings in fact. "
    "Available only when an execution sandbox is provisioned.",
    "verification",
)


def load_prompt(name: str) -> str:
    """Load a top-level prompt file (e.g. the orchestrator slug)."""
    return (PROMPTS_DIR / f"{name}.md").read_text().strip()


def load_specialist_prompt(name: str) -> str:
    """Load a specialist prompt from ``specialists/<name>.md``."""
    return (SPECIALISTS_DIR / f"{name}.md").read_text().strip()


def get_system_prompt() -> str:
    """The orchestrator system prompt, with today's date appended."""
    prompt = load_prompt(SLUG)
    prompt += f"\n\nToday's date is {date.today().isoformat()}."
    return prompt
