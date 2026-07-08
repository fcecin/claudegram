#!/usr/bin/env bash
# install-cron.sh <install-dir> [--remove]
#
# Install (or with --remove, remove) THIS claudegram install's wake cron: every 3 hours
# (0,3,6,9,12,15,18,21) run ./cg-wake, which injects a heartbeat turn into the current bot.
#
# Idempotent + per-install: keyed by a unique marker (the install's identity slug), so a
# reinstall replaces only THIS install's line and never disturbs other installs' entries.
# Overridable for tests: $CRONTAB_BIN (default 'crontab') and $WAKE_CRON_SLUG (default the
# instance_id.py slug) let a test drive it against a fake crontab with no real side effect.
set -eu   # NOT pipefail: the grep filters below legitimately return 1 when nothing matches

DIR="${1:-}"
[ -n "$DIR" ] || { echo "usage: install-cron.sh <install-dir> [--remove]" >&2; exit 2; }
DIR="$(cd "$DIR" && pwd)"
REMOVE=0
[ "${2:-}" = "--remove" ] && REMOVE=1

CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
if ! command -v "${CRONTAB_BIN%% *}" >/dev/null 2>&1; then
  echo "crontab not found (${CRONTAB_BIN}); skipping wake cron." >&2
  exit 0
fi

SLUG="${WAKE_CRON_SLUG:-$(python3 "$DIR/instance_id.py" desktop_name "$DIR")}"
MARK="# claudegram-wake:${SLUG}"
LINE="0 0,3,6,9,12,15,18,21 * * * cd '${DIR}' && ./cg-wake >/dev/null 2>&1 ${MARK}"

current="$("$CRONTAB_BIN" -l 2>/dev/null || true)"
# keep every line that is neither our marker nor blank. Anchor the marker at end-of-line so a
# slug that is a prefix of another (e.g. removing 'cg' must not clobber 'cg4') can't collide.
kept="$(printf '%s\n' "$current" | grep -vE "claudegram-wake:${SLUG}\$" | grep -vE '^[[:space:]]*$')" || true

if [ "$REMOVE" -eq 1 ]; then
  new="$kept"; action="Removed"
else
  if [ -n "$kept" ]; then new="${kept}"$'\n'"${LINE}"; else new="$LINE"; fi
  action="Installed"
fi

if [ -n "$new" ]; then printf '%s\n' "$new" | "$CRONTAB_BIN" -; else printf '' | "$CRONTAB_BIN" -; fi
echo "${action} wake cron for ${SLUG} (every 3h on 0,3,6,9,12,15,18,21)."
