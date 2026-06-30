# CLAUDE.md — working on claudegram

Guidance for an AI (Claude Code) **developing this repo**. (Not the runtime prompt —
the bridge drives a *separate* Claude Code instance whose cwd is `~/cghome`.)

## What it is
A private Telegram bridge that drives a persistent Claude Code instance by voice/text
from a phone. Three Python files, one venv:
- `gui.py` — **PySide6** system-tray app; supervises `bot.py` as a child (`QProcess`),
  shows a live console, auto-restarts on crash, has the Unblock / Restart / Clear-logs
  buttons. The tray app **is** the supervisor.
- `bot.py` — the bridge: Telegram I/O, local transcription (faster-whisper), the
  firewall, the `bot` commands, rendering, the watchdog, and the `[HARNESS]` channels.
- `claude_driver.py` — `ClaudeController`: owns the Claude Agent SDK client.

## Core model: the bridge is a MONITOR of the Claude instance
`ClaudeController` runs **one always-on reader** (`receive_messages()`), never stopping
at a `ResultMessage`. It routes each *segment* (a turn, delimited by `SystemMessage`
`init` … `ResultMessage`):
- a segment right after one of our `query()`s → **user turn** → `_user_sink` (the
  `SegmentRenderer` for that `dispatch_to_claude` call);
- any other segment → a turn **Claude started on its own** (a background shell finished)
  → `_spontaneous_sink` (`SpontaneousRelay`), rendered to the owner chat.
It also tracks **background shells** from `TaskStartedMessage` / `TaskUpdatedMessage`
(`patch.status==completed`) / `TaskNotificationMessage`, exposed via `controller.status()`.

Key SDK fact (proven): when a turn ends with a `run_in_background` shell still running,
the Claude instance **wakes itself and emits a new turn when the shell completes** — no
user message needed. The old single-`receive_response()` design dropped those; the
continuous reader relays them.

## Rendering (bot.py)
- `StatusBoard` — one message edited in place for the live activity feed. **Telegram
  edits in place**, so the board must stop mutating once the answer streams below it:
  `seal()` is called on the first answer delta. Never un-seal.
- `ParagraphStreamer` — streams the answer at blank-line breaks with a ~3s Nagle
  coalesce window. A Python-injected **`[[END]]`** marks the end of a turn = *the prompt
  is free for input* (orthogonal to shells — do NOT gate it on background work).
- Mashing fix: a tool/thinking between two text blocks would concatenate them
  (`…background:Confirmed…`); the renderer re-inserts `\n\n` (`text_interrupted`).
- `watchdog_loop` — every ~60s of Telegram **silence**, posts the instance state:
  `working|idle` PLUS `N shells (what) | no shells`. Speaks only on silence (tracked by
  `mark_sent()` on every NEW message — edits don't count), declares dead-idle once.

## `[HARNESS]` channels (IPC, both directions)
- **machine → phone**: drop a file in `outbox/` (atomic rename) → `harness_outbox_loop`
  relays it as `🤖 [HARNESS] …`. Helper: `./cg-notify "msg"`.
- **phone → machine/AI**: `bot harness <msg>` / `bot h <msg>` writes to `inbox/`. Helper:
  `./cg-inbox` (drain), `--peek`, or `--wait` (block until one; loop primitive).

## Sleep mode (distinct from lock/kill)
`bot sleep` writes `SLEEP.flag`; while it exists, `handle_text`/`handle_audio` ignore
**all** Telegram input (even `bot` commands) and reply `SLEEP_MSG` — but Claude keeps
running (background work continues). The ONLY exit is the tray's **WAKE UP** button
(`gui.py` deletes `SLEEP.flag`, watched on the 2s timer). Not a security state (unlike
the firewall lock) and doesn't kill anything (unlike `bot kill`).

## Firewall
Lean guard preamble per prompt; a genuine malicious request makes Claude reply leading
with `HACKING ATTEMPT BLOCKED` + reason → bridge writes `BLOCKED.flag` (hard lock) until
cleared at the tray. **Keep the guard small** (no prompt bloat). False positives go to
`HACKING_REGRESSIONS.md` (read on demand, not injected). Allowlist is the real access
control; subscription is forced (`force_subscription_env` strips `ANTHROPIC_API_KEY`).

## DEPLOY (non-obvious)
- `bot.py` / `claude_driver.py` change → restart **just the bot child**: find its PID
  (`ps -eo pid,cmd | grep '[/]claudegram/bot.py'`) and `kill <pid>`; the tray supervisor
  respawns it and the session resumes (`session.id`).
- `gui.py` change → **full tray restart** (quit tray + relaunch `./run-gui.sh`).
- **NEVER** `pkill -f "claudegram/bot.py"` — the pattern matches the killing shell's own
  argv and it self-kills. Kill by explicit PID.

## TESTING (do not break the user's live session)
- **Never** run a turn against the module-level `controller` — it writes the real
  `session.id`. Use a throwaway `ClaudeController(temp_cwd, temp_session_file, …)`, or
  monkeypatch `bot.controller` with a fake bot that records `send_message`/
  `edit_message_text`. Use `effort=low` for speed. See the integration pattern: fake bot
  + isolated controller + a `run_in_background` shell to exercise the spontaneous relay.

## Logging
Everything (request / thinking / each tool + result / answer / blocks) → `claudegram.log`
(tray "Clear logs" truncates it). Disk is cheap here; log generously.
