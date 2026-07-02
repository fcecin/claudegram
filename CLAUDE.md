# CLAUDE.md — working on claudegram

Guidance for an AI (Claude Code) **developing this repo**. (Not the runtime prompt —
the bridge drives a *separate* Claude Code instance whose cwd is `~/cghome`.)

> ## 🤝 If you're a Claude that just opened this repo
> claudegram is a Telegram⇄Claude bridge. It is **self-sufficient**: the tray supervises and
> auto-restarts `bot.py`, the watchdog self-heals and nudges, transcription can't wedge it, and
> Claude extends itself on request — so the optional **harness** (an external Claude that
> watches the owner's `bot harness` inbox and operates the bridge) is *not* required. Don't
> push it. If the user asks for a harness, the role is just this repo's knowledge — read
> [`harness-charter.md`](harness-charter.md) and run the `cg-inbox --wait → cg-notify ack → act`
> loop, obeying its security rules. Otherwise carry on as a normal dev session.

## What it is
A private Telegram bridge that drives a persistent Claude Code instance by voice/text/images
from a phone. Four Python files, one venv:
- `gui.py` — **PySide6** system-tray app; supervises `bot.py` as a child (`QProcess`),
  shows a live console, auto-restarts on crash. Controls: Restart / Unblock / Unblock+regress /
  WAKE UP / Clear logs, and the **🛡 Intrusion Lock** on/off switch (right side). The tray app
  **is** the supervisor.
- `bot.py` — the bridge: Telegram I/O, the firewall + intrusion lock, the `bot` commands,
  rendering, the watchdog, and the `[HARNESS]` channels. It launches transcription as a
  subprocess and no longer loads whisper itself (lean event-loop process).
- `transcribe_worker.py` — standalone, **killable** faster-whisper decoder, run as a child
  process per voice message (a thread can't be killed; a process can).
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
  below it via `is_latest=False`) starts a fresh message. Silence tracked by `mark_sent()` on
  every NEW message (edits don't count). **3-state idle handling:** `×IDLE_SHELLS_NUDGE_AT`
  (30) idle+shells ticks → `enqueue_for_claude(IDLE_SHELLS_NUDGE)` (continue / check stuck
  shells / clean up); `×IDLE_NO_SHELLS_NUDGE_AT` (30) idle+**no**-shells ticks →
  `IDLE_NO_SHELLS_NUDGE` ("continue, or reply starting with `NO MORE WORK`"); if Claude's reply
  leads with `NO_MORE_WORK_MARKER` (detected in `SegmentRenderer.finalize` → `set_no_more_work`,
  cleared on the next user message) → **IDLE_DONE** (one-shot terminal notice, no more nudging).
  The loop is fully wrapped (`except: log`) so it can't die silently, and it **skips entirely
  while `transcribe_active()`** (a decode isn't idleness).

## Voiceback (spoken replies) — a persistent toggle, Kokoro TTS
`bot voice on`/`off` toggles `VOICE_MODE_FILE` (`voice.mode`, presence = on; `voice_mode_on()`),
and the dispatcher OR's it into `voiceback` for every turn. When on: `build_prompt` injects
`VOICEBACK_PREAMBLE` (be brief, no code/paths/lists — the whole reply is spoken), the
`SegmentRenderer` does NOT stream (collects `answer_buf`), and `_finalize_voiceback` sends the
whole answer as ONE voice message (no text transcript) + `[[END]]`.
- **TTS = Kokoro** (offline ONNX, no torch, no network): `models/kokoro-v1.0.onnx` +
  `voices-v1.0.bin` (gitignored, fetched by `./fetch-kokoro.sh`; `pip install kokoro-onnx
  soundfile`). `synthesize_voice` → wav → ffmpeg → ogg/opus. Lazy-loaded singleton `_get_kokoro`.
- A session's `voice` config selects a Kokoro voice + optional ffmpeg effects (`_voice_filters`).
  `_resolve_voice` switches to a native voice for non-English text (pt/es/fr/it/hi/ja/zh); with no
  `voice` config a session uses `DEFAULT_VOICE`.
- `voiceback: false` in a session's config opts it out entirely — it never speaks even when
  voiceback is globally on (`dispatch_to_claude` forces it off). For image-only sessions.

## Transcription (killable subprocess + watchdog)
Voice → `handle_audio` downloads the audio, then runs the decode as a **subprocess**
(`asyncio.create_subprocess_exec(sys.executable, "transcribe_worker.py", path, …)`), NOT a
thread — so a stalled/looping whisper can be killed. The worker streams `PROGRESS <pct> <eta>`
on stdout (consumed live by `_read_worker`, shown in the bubble), then `RESULT <json>` / `ERROR`.
A budget watchdog (`asyncio.wait_for(_read_worker(), timeout = max(120, audio×6))`) kills a
runaway and replies "stalled — resend". The bubble clock is a SEPARATE `_spawn`'d heartbeat on a
fixed 10s timer (moving datetime = event loop alive), independent of the decode; both wrapped so
neither dies silently. `condition_on_previous_text=False` disarms whisper's repetition loop.
Quality is live: `bot transcribe best|good|fast` → `compute.type` (float32 / int8_float32 /
int8), passed per-spawn via `env={…WHISPER_COMPUTE_TYPE…}`. The parent holds no whisper model;
audio + images are swept at startup.
**NEVER run `py-spy`/ptrace on the LIVE bot** — it stops every thread (event loop included) and
froze a live transcription. Diagnose with the bot's logging + `kill -USR1 <pid>` instead.

## Photo / image input
`handle_photo` (`filters.PHOTO | filters.Document.IMAGE`) downloads to `IMAGE_TMP`, prunes
images >6h old, and `enqueue_for_claude`s a prompt pointing Claude at the path (+ caption if any)
— Claude reads it with the `Read` tool (multimodal in; no transcription). Files persist until the
later turn reads them; swept at startup. `source="image"` collapses to the text guard.

## Intrusion lock (paranoid tripwire — default ON, GUI-only)
Any message from a non-allowlisted id → `handle_intrusion`: log it, and (if `intrusion_gate_on()`)
**hard-lock** (`controller.kill()` + `engage_block`) + DM the owner; the intruder gets no reply;
idempotent if already locked. Wired into every entry point (text/audio/photo/`/start`/new/stop/
status). Gated by `INTRUSION_OFF_FILE` (`INTRUSION_OFF.flag`, presence = OFF; **default ON** =
absent). The toggle lives ONLY in the GUI (`gui.py` 🛡 switch creates/deletes the flag) — **never
a `bot` command**, so it can't be disabled remotely over Telegram (like physical-unlock-only).

## Multi-session multiplexing (ONE Telegram bot, N concurrent sessions)
One bot can drive several independent Claude sessions in the same channel — an alternative to the
single default session. `SessionRegistry`/`Session` give each its own `ClaudeController` (own
`session.<name>.id`/`cwd`/`effort` files; the DEFAULT `claude` reuses the original `session.id`),
its own dispatch queue+worker, watchdog, and spontaneous relay — so sessions run **concurrently**.
Each session has a color badge. A session may carry a `config.json` (read by `bot_config`); the
per-session contents are not documented here.
- **Progressive disclosure:** while only `claude` exists, `registry.multiplexing()` is False and
  every `registry.badge(session)` is `""` — the bridge renders exactly like the single-session
  version. A 2nd session flips multiplexing on and every bot-authored artifact (board header,
  each streamed answer message, `[[END]]`, alerts, watchdog line, transcription bubble, voiceback)
  is prefixed `"<emoji> <name> · "` so an interleaved scroll self-identifies per message. You
  CANNOT edit the user's own messages via the Bot API, so the tag rides the bot's reply-anchored
  ack (the board replies to your message), never your message.
- **Routing:** `bot select <name>` (aliases switch/use) creates-on-first-use + sets the CURRENT
  session (reassigns the module `controller`); bare input goes to current. Handlers capture
  `target = registry.current()` at send-time (a mid-decode select can't misroute). `bot sessions`
  lists; `bot end <name>` tears one down (never the default).
- **Per-session vs global:** each session owns its queue/worker/watchdog/relay AND its own
  `no_more_work` flag (`set_no_more_work(session, …)`). GLOBAL stays global because it's about the
  channel/machine: the `_transcribe_lock` (one whisper at a time — CPU), all security state
  (allowlist, intrusion lock, `BLOCKED.flag`, `SLEEP.flag`), and `_last_tg_send` silence. Intrusion
  kills EVERY session's controller. `mark_sent()`/`Watchdog._touch()` invalidate all watchdogs'
  `is_latest`. One `worker_guard` covers all sessions.

## Message batching
Handlers don't call `dispatch_to_claude` directly — they `enqueue_for_claude(session, …)`. A
per-session `session_worker` (started via `_activate_session` in on_startup / on `bot select`)
drains that session's queue after a `BATCH_DEBOUNCE` window and sends the WHOLE burst as ONE
combined prompt (`\n\n`-joined). voiceback/source are OR'd across the batch. This also serializes
that session's user turns (one at a time); messages arriving mid-turn batch into the next. `bot
compact` still dispatches directly to the current session (serialized by the controller lock).

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

## Harness (external operator — OPTIONAL/dispensable)
The bridge is self-sufficient, so the harness is **optional** (the owner may run without one;
don't assume there is one). `run-harness.sh` opens a visible terminal running a Claude Code
instance pre-prompted by `harness-charter.md` to operate/improve claudegram and serve the
`bot harness` inbox
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
- `bot.py` / `claude_driver.py` / `transcribe_worker.py` change → restart **just the bot
  child**: find its PID (`ps -eo pid,cmd | grep '[/]claudegram/bot.py'`) and `kill <pid>`; the
  tray supervisor respawns it and the session resumes (`session.id`). (`transcribe_worker.py` is
  re-read per spawn, so its changes apply on the next voice message even without a restart.)
- `gui.py` change → **full tray restart**: kill the tray + bot child, then `./run-gui.sh`. It
  **self-backgrounds** (re-execs via `setsid`, survives terminal close) and gui.py is
  single-instance. From a non-graphical context, pass the desktop env (`WAYLAND_DISPLAY`,
  `DISPLAY`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`). Don't do it mid Claude-turn /
  transcription — wait for idle.
- **Launch paths differ:** manual = `./run-gui.sh` (tray, self-backgrounding) or `./run.sh`
  (headless bot, no tray); **autostart = `./install-autostart.sh`** writes a `.desktop` that
  runs `gui.py` *directly* at login (GNOME-managed) — not `run-gui.sh`. Keep these in sync.
- **NEVER** `pkill -f "claudegram/bot.py"` — the pattern matches the killing shell's own
  argv and it self-kills. Kill by explicit PID.

## TESTING (`./test.sh` — offline, no Telegram, no real Claude)
Run `./test.sh` (→ `tests/run.py`, a deps-free runner; no pytest). It's fast + deterministic
because the bridge has exactly **two external edges** and `tests/fakes.py` fakes BOTH:
- **Telegram** → `FakeBot`/`FakeApp` record `send_message`/`edit_message_text`/… (no token,
  no network). Assert against `fakebot.sent`.
- **Claude** → `FakeController` whose `ask()` drives a **scripted list of SDK messages** into
  the renderer (factories: `sys_init`/`stream_text`/`assistant_tool`/`result_msg`/…). Lets you
  provoke the rare/nondeterministic paths on demand — firewall sentinel trip, the self-started
  (spontaneous) turn, rate-limit, `is_error`, `NO MORE WORK` (incl. under voiceback). Whole
  turns run via `dispatch_to_claude(ctx, session, …)` with a fake session.
Coverage: `test_multiplex` (registry/routing/badges), `test_render` (badge on every message),
`test_regressions` (the 2026-07-01 voiceback bugs, pinned), `test_turn` (firewall / spontaneous
relay / crash via mock-Claude). The runner resets the registry + clears flags between tests.
- **Add a test** whenever you touch bridge logic — it's cheap now. Keep the few *real*-Claude
  checks (isolated `ClaudeController(temp_cwd, temp_session_file, effort='low')`) as a thin
  "contract" layer that pins SDK message shapes + the self-wake-on-shell-complete fact; the
  fakes assume those shapes, so a contract test catches an SDK change the fakes would miss.
- **Never** run a turn against the module-level `controller` — it writes the real `session.id`.

## Logging
Everything (request / thinking / each tool + result / answer / blocks) → `claudegram.log`
(tray "Clear logs" truncates it). Disk is cheap here; log generously.
