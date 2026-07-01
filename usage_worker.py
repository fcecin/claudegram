#!/usr/bin/env python3
"""Standalone /usage scraper for claudegram.

Boots a THROWAWAY `claude` TUI inside tmux, types `/usage`, scrapes the rendered
panel, and prints one line of JSON:

    {"session_pct": 14, "session_reset": "2:10am", "week_pct": 4,
     "week_reset": "Jul 7, 6:59pm", "ts": 1782884000}

Why this exists: the subscription 5h/week utilisation is NOT exposed to the
Agent SDK (the CLI sends `utilization=None` while `status=allowed`), and the
`anthropic-ratelimit-unified-*` headers live inside the CLI subprocess, out of
our reach. But `/usage` renders the numbers as plain text — so we read them the
same way a human does. No prompt is ever sent, so NO tokens are spent; it uses
the existing subscription login and reports the same account-wide numbers as any
other session. The tmux session is always killed on exit.

Run:  python usage_worker.py           (prints JSON, exit 0 on success)
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _hours_until(reset_str, now=None):
    """Convert a scraped reset string to HOURS from now (float, 1 decimal) or None.

    Handles the two `/usage` forms — time-only "2:09am" (next occurrence) and
    dated "Jul 7, 6:59pm" (this year, or next if already past). The panel prints
    times in the machine's local tz, so naive local datetimes line up correctly
    and no timezone handling is needed. Regex (not strptime) to dodge locale/case."""
    if not reset_str:
        return None
    now = now or datetime.datetime.now()
    s = reset_str.strip().lower()
    m = re.search(r"([a-z]{3})\s+(\d{1,2}),?\s+(\d{1,2}):(\d{2})\s*([ap])m", s)  # dated
    if m:
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        hour = int(m.group(3)) % 12 + (12 if m.group(5) == "p" else 0)
        try:
            dt = datetime.datetime(now.year, mon, int(m.group(2)), hour, int(m.group(4)))
        except ValueError:
            return None
        if dt <= now:
            dt = dt.replace(year=now.year + 1)
        return round((dt - now).total_seconds() / 3600, 1)
    m = re.search(r"(\d{1,2}):(\d{2})\s*([ap])m", s)  # time-only
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
        dt = now.replace(hour=hour, minute=int(m.group(2)), second=0, microsecond=0)
        if dt <= now:
            dt += datetime.timedelta(days=1)
        return round((dt - now).total_seconds() / 3600, 1)
    return None

CWD = os.path.expanduser("~/.cache/cg-usage")   # dedicated dir; trust accepted once
SESSION = f"cgusage-{os.getpid()}"
BOOT_TIMEOUT = 45      # seconds to wait for the TUI / trust dialog
PANEL_TIMEOUT = 20     # seconds to wait for the /usage panel to render


def _tmux(*args, check=False):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def _pane() -> str:
    r = _tmux("capture-pane", "-t", SESSION, "-p")
    return r.stdout if r.returncode == 0 else ""


def _parse(text: str) -> dict:
    """Pull `NN% used` + `Resets …` from under the Current session / week headers."""
    def grab(label: str):
        m = re.search(
            re.escape(label) + r".*?(\d+)%\s*used.*?Resets\s+([^\n(]+)",
            text, re.S | re.I,
        )
        if not m:
            return None, None
        return int(m.group(1)), m.group(2).strip()

    s_pct, s_reset = grab("Current session")
    w_pct, w_reset = grab("Current week")
    return {
        "session_pct": s_pct, "session_reset": s_reset,
        "session_hours": _hours_until(s_reset),
        "week_pct": w_pct, "week_reset": w_reset,
        "week_hours": _hours_until(w_reset),
        "ts": int(time.time()),
    }


def scrape() -> dict:
    os.makedirs(CWD, exist_ok=True)
    _tmux("kill-session", "-t", SESSION)  # clear any stale session with our name
    _tmux("new-session", "-d", "-s", SESSION, "-c", CWD, "-x", "220", "-y", "55",
          check=True)
    try:
        _tmux("send-keys", "-t", SESSION, "claude", "Enter")

        # 1) wait for the trust dialog (accept it) or the input prompt
        trusted = False
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline:
            time.sleep(1)
            p = _pane()
            if not trusted and re.search(r"trust this folder", p, re.I):
                _tmux("send-keys", "-t", SESSION, "Enter")
                trusted = True
                continue
            if re.search(r'for shortcuts|auto mode|Try "', p):
                break

        # 2) type /usage and submit
        _tmux("send-keys", "-t", SESSION, "C-u")
        time.sleep(0.5)
        _tmux("send-keys", "-t", SESSION, "/usage")
        time.sleep(1.5)
        _tmux("send-keys", "-t", SESSION, "Enter")

        # 3) wait for the panel, then scrape
        text = ""
        deadline = time.time() + PANEL_TIMEOUT
        while time.time() < deadline:
            time.sleep(1)
            text = _pane()
            if re.search(r"Current session", text) and re.search(r"Current week", text):
                break
        return _parse(text)
    finally:
        _tmux("kill-session", "-t", SESSION)


def main() -> int:
    try:
        data = scrape()
    except Exception as e:  # never crash the caller; emit an error record
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(data))
    # success only if we actually got both numbers
    return 0 if data.get("session_pct") is not None and data.get("week_pct") is not None else 2


if __name__ == "__main__":
    sys.exit(main())
