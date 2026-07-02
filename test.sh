#!/usr/bin/env bash
# Run the offline test suite: no Telegram, no real Claude, no token/GPU. Fast + deterministic.
# Fakes both external edges (Telegram bot object + ClaudeController). See tests/ and CLAUDE.md.
set -euo pipefail
cd "$(dirname "$0")"

# Prefer the project venv (has telegram / claude_agent_sdk / gtts / langdetect); else python3.
PY="./.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
    echo "No Python found (looked for ./.venv/bin/python and python3)." >&2
    exit 2
fi

exec "$PY" tests/run.py "$@"
