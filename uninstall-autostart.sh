#!/usr/bin/env bash
# Stop THIS claudegram install from starting at login (does not stop a running instance).
# Resolves the same per-install .desktop name as install-autostart.sh via instance_id.py, so
# running it from a copy's directory removes that copy's entry — not some other install's.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP="$(python3 "$DIR/instance_id.py" desktop_name "$DIR")"
FILE="$DEST/$DESKTOP.desktop"

if [ -f "$FILE" ]; then
    rm -f "$FILE"
    echo "Removed autostart entry: $FILE"
else
    echo "No autostart entry found at $FILE"
fi
