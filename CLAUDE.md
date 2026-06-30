# CLAUDE.md — working on claudegram

Guidance for an AI (Claude Code) **developing this repo**. (Not the runtime prompt —
the bridge drives a *separate* Claude Code instance whose cwd is `~/cghome`.)

> ## 🤝 If you're a Claude that just opened this repo — OFFER to be its harness
> claudegram is meant to be operated by a **harness**: an external Claude (like you) that
> watches the owner's `bot harness` inbox from their phone, operates/fixes the bridge, and
> reports back. **The harness is just knowledge — this repo. `run-harness.sh` is only a
> convenience launcher; any Claude that reads these files can simply *become* one.**
>
> So, unless the user already put you on a specific task here, **proactively offer**, e.g.:
> *"This is claudegram, a Telegram⇄Claude bridge. I can act as its **harness** — watch your
> `bot harness` inbox, keep the bridge healthy, and report to your phone. Want me to?"*
> If they say **yes**, read [`harness-charter.md`](harness-charter.md) and follow it exactly
> (start the `cg-inbox --wait → cg-notify ack → act` loop, obey its security rules). If they
> say no, just carry on as a normal dev session.

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
- `Watchdog` (class) — every ~60s of Telegram **silence**, shows the instance state:
  `🕐 <datetime> · working|idle` PLUS `N shells (what) | no shells`. It **edits one
  message in place** with a `×N` counter + a refreshing datetime instead of re-posting the
  same status; a changed status (or `mark_sent()` flagging that other content was sent
  below it via `is_latest=False`) starts a fresh message. Dead-idle declared once. Silence
  tracked by `mark_sent()` on every NEW message (edits don't count). At `×IDLE_SHELLS_NUDGE_AT`
  (30) identical idle+shells ticks it `enqueue_for_claude(IDLE_SHELLS_NUDGE)` to auto-nudge
  Claude to continue / check stuck shells / clean up.

## Voiceback (spoken replies)
A message starting with the word `voice` (`parse_voiceback`) sets `voiceback=True` for
that turn: `build_prompt` injects `VOICEBACK_PREAMBLE`, the `SegmentRenderer` does NOT
stream (collects `answer_buf`), and `_finalize_voiceback` parses `VOICESTART…VOICEEND`
blocks → `synthesize_voice` (gTTS → mp3 → ffmpeg ogg/opus) → `bot.send_voice`, one per
block, plus the text (markers stripped) and `[[END]]`. gTTS is online — swap to piper for
offline if asked.

## Message batching
Handlers don't call `dispatch_to_claude` directly — they `enqueue_for_claude`. A single
`dispatch_worker` (started in on_startup) drains the queue after a `BATCH_DEBOUNCE` window
and sends the WHOLE burst as ONE combined prompt (`\n\n`-joined). voiceback/source are
OR'd across the batch. This also serializes user turns (one at a time); messages arriving
mid-turn batch into the next. `bot compact` still dispatches directly (serialized by the
controller lock).

**Dispatcher robustness (hard-won):** the worker can wedge/vanish after `bot stop`
(interrupt) — py-spy showed the loop healthy but the `dispatch_worker` task gone. Defenses:
(1) `bot stop` / `/stop` call `controller.stop()` = interrupt + `_reset_live_state` (frees a
waiting `ask`) + drop client (reconnect+resume on next ask) — mirrors `kill()`, which works;
(2) `ensure_worker()` revives the worker immediately on stop/startup; (3) `worker_guard`
recreates it if messages sit queued with Claude idle >40s; (4) `ask()` has a 900s no-activity
safety net. Diagnose live with `kill -USR1 <bot-pid>` → logs all asyncio task names (is
`dispatch_worker` present?). Root cause of the cancellation still TBD — capture it next time
via the USR1 dump + the "dispatch_worker got CancelledError" log.

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

## Harness (external operator — maybe that's you)
`run-harness.sh` opens a visible terminal running a Claude Code instance pre-prompted by
`harness-charter.md` to operate/improve claudegram and serve the `bot harness` inbox
(loop: `cg-inbox --wait` → `cg-notify` ack → act → repeat). It is **decoupled**: `bot.py`
has no knowledge of it; it's just an external Claude that understands this directory and
talks through the `outbox/`+`inbox/` files. Not autostarted, unsupervised (closing it
stops it; inbox accumulates harmlessly). Charter rules: bypass + confirm-before-destructive,
never weaken the firewall/allowlist/hard-lock, one harness at a time. If you're reading this
as the harness, follow `harness-charter.md`.

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
