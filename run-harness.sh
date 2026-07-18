#!/usr/bin/env bash
# Launch the claudegram HARNESS: a VISIBLE terminal running a Claude Code instance,
# pre-prompted (HARNESS_CHARTER.md) to operate this claudegram install and serve the
# `bot harness` inbox from your phone.
#
# By design this is standalone and UNSUPERVISED — it's just "a Claude that understands the
# claudegram directory", like a human-started dev session. Close the window and the harness
# simply stops; bot.py neither knows nor cares, and inbox messages safely accumulate in
# inbox/ until you run a harness again. Run only ONE harness at a time (two would race on
# cg-inbox). Not autostarted — you run this when you want a harness.
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI not found on PATH. Install/login Claude Code first." >&2
  exit 1
fi
if [ ! -f "$DIR/HARNESS_CHARTER.md" ]; then
  echo "Missing $DIR/HARNESS_CHARTER.md" >&2
  exit 1
fi

KICK='You are the claudegram harness. Read ./HARNESS_CHARTER.md now and follow it exactly, then begin your inbox monitor loop.'
# The command the terminal will run: cd into the install dir, then start an interactive,
# autonomous Claude with the kickoff prompt (it reads the charter and starts its loop).
CMD="cd \"$DIR\" && exec claude --dangerously-skip-permissions \"$KICK\""

if command -v gnome-terminal >/dev/null 2>&1; then
  exec gnome-terminal --title="claudegram harness" -- bash -lc "$CMD"
elif command -v konsole >/dev/null 2>&1; then
  exec konsole --title "claudegram harness" -e bash -lc "$CMD"
elif command -v xfce4-terminal >/dev/null 2>&1; then
  exec xfce4-terminal --title="claudegram harness" -e "bash -lc '$CMD'"
elif command -v xterm >/dev/null 2>&1; then
  exec xterm -T "claudegram harness" -e bash -lc "$CMD"
else
  echo "No terminal emulator found (tried gnome-terminal, konsole, xfce4-terminal, xterm)." >&2
  echo "Run the harness manually:  cd \"$DIR\" && claude --dangerously-skip-permissions \"\$KICK\"" >&2
  exit 1
fi
