#!/usr/bin/env bash
# Make claudegram (tray app) start automatically when you log in.
#
# PER-INSTALL: run this from EACH copy's directory and each gets its OWN autostart entry
# (distinct .desktop filename + Name + WM class), so several claudegram installs can all
# autostart without clobbering one another. The identity mirrors instance_id.py: the
# directory basename with a redundant 'claudegram-' prefix stripped, or instance.txt line 1.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
BASE="$(basename "$DIR")"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

# --- resolve this install's label + .desktop slug (mirror of instance_id.py) --------
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
    DESKTOP="claudegram"; NAME="claudegram"
else
    SLUG="$(printf '%s' "$LABEL" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' \
            | sed 's/--*/-/g; s/^-//; s/-$//')"
    [ -n "$SLUG" ] || SLUG="claudegram"
    DESKTOP="claudegram-$SLUG"; NAME="claudegram ($LABEL)"
fi
FILE="$DEST/$DESKTOP.desktop"

mkdir -p "$DEST"
cat > "$FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$NAME
Comment=Voice-to-text Telegram bot (tray)
Exec=$DIR/.venv/bin/python $DIR/gui.py
Icon=audio-input-microphone
StartupWMClass=$DESKTOP
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
Categories=Utility;
EOF

echo "Installed autostart entry: $FILE   (Name: $NAME)"
echo "It will launch on your next login. To start it now: ./run-gui.sh"
