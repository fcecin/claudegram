#!/usr/bin/env bash
# Stop claudegram from starting at login (does not stop a running instance).
set -euo pipefail
FILE="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/claudegram.desktop"
if [ -f "$FILE" ]; then
    rm -f "$FILE"
    echo "Removed autostart entry: $FILE"
else
    echo "No autostart entry found at $FILE"
fi
