FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY agents/smart-code-reviewer-y7b4ip/pyproject.toml agents/smart-code-reviewer-y7b4ip/uv.lock ./
RUN uv sync --frozen --no-cache --no-dev

COPY agents/smart-code-reviewer-y7b4ip/src/ ./src/

RUN useradd -m -u 1000 appuser
USER appuser

EXPOSE 8080

CMD ["uv", "run", "opentelemetry-instrument", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
