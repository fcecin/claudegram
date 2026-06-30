#!/usr/bin/env bash
# Make claudegram (tray app) start automatically when you log in.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
FILE="$DEST/claudegram.desktop"

mkdir -p "$DEST"
cat > "$FILE" <<EOF
[Desktop Entry]
Type=Application
Name=claudegram
Comment=Voice-to-text Telegram bot (tray)
Exec=$DIR/.venv/bin/python $DIR/gui.py
Icon=audio-input-microphone
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
Categories=Utility;
EOF

echo "Installed autostart entry: $FILE"
echo "It will launch on your next login. To start it now: ./run-gui.sh"
