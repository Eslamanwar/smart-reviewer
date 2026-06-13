#!/usr/bin/env bash
#
# review.sh — ask the running smart-code-reviewer agent to review a PR and
# print the extracted markdown report (the assistant text only — tool calls
# and thinking events are dropped).
#
# Usage:
#   ./review.sh <pr-reference> [output-file]
#
# Examples:
#   ./review.sh https://github.com/langchain-ai/deepagents/pull/3936
#   ./review.sh langchain-ai/deepagents#3936 review.md
#
# Env vars:
#   AGENT_URL    full invocations URL (default: http://localhost:8080/invocations)
#   USER_EMAIL   forwarded_props.user_email (default: $USER@org.com)
#
set -euo pipefail

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

PR_REF="$1"
OUT_FILE="${2:-}"
AGENT_URL="${AGENT_URL:-http://localhost:8080/invocations}"
USER_EMAIL="${USER_EMAIL:-${USER:-anon}@org.com}"

# Build the AG-UI request body. jq -Rs reads the PR reference as a raw string
# so quoting/escaping is handled correctly.
BODY=$(jq -nc \
  --arg content "Review $PR_REF" \
  --arg email "$USER_EMAIL" \
  '{
    thread_id: "t1",
    run_id: "r1",
    messages: [{id: "m1", role: "user", content: $content}],
    tools: [],
    context: [],
    state: {},
    forwarded_props: {user_email: $email}
  }')

# Stream the SSE response and reassemble TEXT_MESSAGE_CONTENT deltas into the
# original markdown. -j = no separator between deltas; -r = raw output.
extract() {
  grep '^data:' \
    | sed 's/^data: //' \
    | jq -rj 'select(.type=="TEXT_MESSAGE_CONTENT") | .delta'
}

run() {
  curl -N -s "$AGENT_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: text/event-stream' \
    -d "$BODY" \
    | extract
}

if [[ -n "$OUT_FILE" ]]; then
  run > "$OUT_FILE"
  echo >> "$OUT_FILE"
  echo "Review written to $OUT_FILE" >&2
else
  run
  echo
fi
