#!/usr/bin/env bash
# Make claudegram (tray app) start automatically when you log in.
#
# PER-INSTALL: run this from EACH copy's directory and each gets its OWN autostart entry
# (distinct .desktop filename + Name + WM class), so several claudegram installs can all
# autostart without clobbering one another. The identity (label / .desktop slug) comes from
# instance_id.py — the SAME resolver the tray uses (instance.json > instance.txt > dir name) —
# so the shell never drifts from the code.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

DESKTOP="$(python3 "$DIR/instance_id.py" desktop_name "$DIR")"   # claudegram | claudegram-<slug>
NAME="$(python3 "$DIR/instance_id.py" title "$DIR")"             # claudegram | claudegram · <label>
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

# Also (re)install this install's wake cron: every 3h it injects a heartbeat turn into the
# current bot. Per-install + idempotent (see install-cron.sh); never touches other installs.
"$DIR/install-cron.sh" "$DIR" || echo "wake cron step skipped (crontab unavailable?)" >&2

echo "It will launch on your next login. To start it now: ./run-gui.sh"
