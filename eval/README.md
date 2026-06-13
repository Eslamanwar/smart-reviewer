# Eval — LLM as judge

Measures how good this agent's reviews are by comparing them against **real human
reviews** on merged PRs. The human review is treated as ground truth; an LLM judge
scores how much of it the agent independently caught (**recall**) plus the agent's
standalone **quality**.

## Files

| File | What |
|------|------|
| [dataset.yml](dataset.yml) | The PRs under eval — each has a substantive human review. `note`/`dimension` are for us, **not** shown to the judge. |
| [judge_prompt.md](judge_prompt.md) | The judge rubric (extract human findings → score agent coverage → rate quality → emit JSON). |
| [run_eval.py](run_eval.py) | The runner: agent → fetch human review → judge → scorecard. |
| `results/` | Per-PR scorecards (`.json` + `.md`). Git-ignored. |

## Run it

From the repo root, with `OPENROUTER_API_KEY` in your `.env`:

```bash
uv run python eval/run_eval.py                  # full dataset
uv run python eval/run_eval.py --limit 1        # just the first PR
uv run python eval/run_eval.py --pr psf/requests#3789   # one ad-hoc PR
```

No key yet? Validate the plumbing without calling any model — this fetches and prints
the human reviews the judge would use:

```bash
uv run python eval/run_eval.py --dry-run
```

## How scoring works

For each PR the judge:
1. **Extracts** the humans' substantive technical findings (ignoring "+1", "rebase",
   release chatter, prose typos).
2. Labels each **CAUGHT / PARTIAL / MISSED** by the agent → a recall score
   (partial = 0.5).
3. Lists the agent's **extra** findings and judges each **valid / questionable / noise**.
4. Rates **correctness, specificity, actionability, overall** (1–10).

Output is strict JSON, rendered to a Markdown scorecard in `results/` and a summary
table on stdout.

## Notes & caveats

- **Judge model** defaults to `google/gemini-2.5-pro` (override with `JUDGE_MODEL`).
  It's deliberately a *different/stronger* model than the agent's default to reduce
  self-preference bias — don't judge with the same model you're reviewing with.
- The human review is **noisy ground truth**: humans miss things and chat about
  logistics. That's why the judge also assesses agent-only findings on their merits
  rather than treating every non-human-mentioned finding as wrong.
- Recall is `null` when a PR's humans left no substantive technical finding.
- Each run hits the model for the full agent review **and** the judge — it costs
  tokens and takes a while per PR.
