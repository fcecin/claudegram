#!/usr/bin/env bash
# Stop THIS claudegram install from starting at login (does not stop a running instance).
# Resolves the same per-install .desktop name as install-autostart.sh (mirror of instance_id.py),
# so running it from a copy's directory removes that copy's entry — not some other install's.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
BASE="$(basename "$DIR")"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

HAS_INSTANCE=0
LABEL="$BASE"
if [ -s "$DIR/instance.txt" ]; then
    HAS_INSTANCE=1
    L="$(grep -m1 '[^[:space:]]' "$DIR/instance.txt" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -n "$L" ] && LABEL="$L"
else
    case "$BASE" in
        claudegram[-_.]?*) LABEL="${BASE#claudegram?}";;
    esac
fi
if [ "$BASE" = "claudegram" ] && [ "$HAS_INSTANCE" -eq 0 ]; then
    DESKTOP="claudegram"
else
    SLUG="$(printf '%s' "$LABEL" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' \
            | sed 's/--*/-/g; s/^-//; s/-$//')"
    [ -n "$SLUG" ] || SLUG="claudegram"
    DESKTOP="claudegram-$SLUG"
fi
FILE="$DEST/$DESKTOP.desktop"

if [ -f "$FILE" ]; then
    rm -f "$FILE"
    echo "Removed autostart entry: $FILE"
else
    echo "No autostart entry found at $FILE"
fi
