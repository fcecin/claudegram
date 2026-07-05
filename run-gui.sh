#!/usr/bin/env bash
# Start the claudegram tray app (which supervises the bot).
#
# SELF-BACKGROUNDING: run `./run-gui.sh` and walk away — it re-execs itself in a new
# session (setsid) with stdio redirected to gui-stderr.log, so closing the terminal can't
# SIGHUP it. The graphical env (WAYLAND_DISPLAY/DISPLAY/XDG_RUNTIME_DIR/DBUS_…) is inherited
# from your desktop session. Re-running is safe: gui.py is single-instance PER DIRECTORY
# (QLocalServer keyed to this install's path), so a second launch of THIS copy just pokes its
# running tray and exits — while a copy in another directory runs its own independent tray.
#
# Creates the virtualenv + installs deps on first run.
set -euo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")"

if [ ! -d .venv ]; then
    echo "Creating virtualenv..."
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi

# First invocation: detach into our own session and hand the terminal back. The re-exec
# sets CLAUDEGRAM_DETACHED so the second pass skips this block and actually runs the app.
if [ -z "${CLAUDEGRAM_DETACHED:-}" ]; then
    CLAUDEGRAM_DETACHED=1 setsid "$SELF" "$@" >> gui-stderr.log 2>&1 < /dev/null &
    echo "claudegram tray launched in background (session $!) — safe to close this terminal."
    echo "logs: $(dirname "$SELF")/gui-stderr.log   ·   stop it from the tray icon → Quit"
    exit 0
fi

exec ./.venv/bin/python gui.py
