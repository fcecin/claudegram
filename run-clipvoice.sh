#!/usr/bin/env bash
# Start clipvoice (open-mic dictation -> clipboard). Shares claudegram's
# virtualenv; creates it + installs deps on first run, exactly like run.sh.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [ ! -d .venv ]; then
    echo "Creating virtualenv..."
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi

exec ./.venv/bin/python clipvoice.py "$@"
