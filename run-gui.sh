#!/usr/bin/env bash
# Start the claudegram tray app (which supervises the bot).
# Creates the virtualenv + installs deps on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Creating virtualenv..."
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi

exec ./.venv/bin/python gui.py
