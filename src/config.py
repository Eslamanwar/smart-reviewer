"""Generated agent configuration — environment variables."""

import os

# OpenRouter (https://openrouter.ai) — a single OpenAI-compatible endpoint
# fronting many providers. Gemini 2.5 Flash is the default: cheap, ~1M-token
# context (handles large PR diffs), and strong at code review. Override any of
# these via env to swap models or point back at another gateway.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
# Accept either the generic key or OpenRouter's conventional env var name.
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "google/gemini-2.5-flash")
# Reasoning effort only applies to reasoning models (OpenAI o-series, Claude
# extended thinking). Empty by default so it isn't sent to models like Gemini
# Flash that reject the param. Set to "low"/"medium"/"high" for a reasoning model.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "")
