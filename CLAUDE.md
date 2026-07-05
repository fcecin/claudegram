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

Two invariants the reader/kill path MUST keep (both were single-session relics that
multiplexing broke — see `tests/test_driver_recovery.py`):
- **`controller.kill()` is PER-SESSION.** It SIGKILLs only *this* controller's own CLI
  subprocess subtree (`sigkill_subtree(self._child_pid)`, pid captured after `connect()`),
  never a sibling's. All sessions' CLI children share `bot.py` as parent, so the old
  process-wide "kill every `claude` child" (`sigkill_claude_subtree`, now the panic-only
  path) nuked the whole fleet — e.g. ending the guard bot killed the main worker.
- **The reader self-heals.** If the CLI dies out from under `_read_loop` (crash, OOM,
  sibling teardown), the loop drops the dead client (`_client = None`, guarded on identity)
  and `_reset_live_state()`s, so the next `ask()` reconnects+resumes and any waiting `ask()`
  is released instead of hanging to the 900s stuck-timeout.

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
  With the **anti-stall guard** on (`bot nostall`), the guard OWNS the idle+no-shells intervention
  instead of the canned nudge (see *Anti-stall guard*). **`bot park`** forces a session into terminal
  idle — no nudging AND no anti-stall policing — cleared on the next user message (`session.parked`).
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
- **Roster is filesystem-driven (no hardcoded palette):** the bot set is scanned from `bots/*/` on
  boot (`discover_bots`). Each bot's DIRECTORY is its definition — its icon, aliases, model, effort,
  and the `internal` flag all live in `bots/<name>/config.json` (+ an optional `main.md`). `rm -rf`
  a bot's dir and it's gone; add a dir and it appears — no code change. `bot_icon`/`selectable_bots`/
  `session_aliases` all read the scan; `Session.emoji` falls back to a gear if a config has no icon.
  `ensure_default_bot()` self-heals the DEFAULT bot's dir + `config.json` + `var/.gitkeep` BEFORE the
  roster is read.
- **Internal/system bots:** `config.json {"internal": true}` = a system bot: it gets a badge but is
  NOT selectable (`resolve_session_name` won't return it), NOT listed in `bot sessions`, and does NOT
  count toward `registry.multiplexing()` (a solo install stays single-session even while one runs in
  the background).
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
  `no_more_work` + `parked` flags (`set_no_more_work(session, …)`). GLOBAL stays global because it's about the
  channel/machine: the `_transcribe_lock` (one whisper at a time — CPU), all security state
  (allowlist, intrusion lock, `BLOCKED.flag`, `SLEEP.flag`), and `_last_tg_send` silence. Intrusion
  kills EVERY session's controller. `mark_sent()`/`Watchdog._touch()` invalidate all watchdogs'
  `is_latest`. One `worker_guard` covers all sessions.

## Anti-stall guard (`bot nostall on|off`) — OPTIONAL, off by default
A global sticky toggle (`nostall.mode`, presence = on; like voiceback). While on, an INTERNAL guard
bot reviews any bot that goes idle with **nothing running** and, if it's stalling, forces it back to
work. bot.py is only PLUMBING — ALL policing logic (what stalling is, how to argue, the output
contract, a self-growing playbook of failure-mode "patches") lives in the guard bot's `main.md` +
its `var/`:
- The watchdog captures each bot's recent answers (`session.recent_answers`). When a bot goes idle +
  no-shells (declared done, or hit the idle threshold), `Watchdog._police_stall` hands those answers
  to the guard via `ask_text` (a quiet turn, no channel render), throttled by a per-bot cooldown.
- **Policing runs OFF the watchdog's critical path** (`_spawn_police`, one consult in flight per bot).
  The guard's review can take a minute; awaiting it inline (the old bug) froze that bot's watchdog —
  no status refresh, no silence tracking — for the whole window, which read as a dead watchdog. Now
  it's fired as a task and the loop keeps ticking. On commit the guard posts a `🐕 anti-stall:
  reviewing…` one-liner **to the owner** (not the reviewed bot), so the reasoning window shows as
  activity; the verdict lands below it.
- The guard replies `NOSTALL_LEGIT_MARKER` (`LEGIT STOP` = release it, let it rest) or a browbeat.
  The browbeat is posted in the watchdog's voice and injected into the stalling bot as a bare
  `[anti-stall]` directive — the bot never converses with the guard, it just acts.
- The guard bot is referenced by NAME (`NOSTALL_BOT`), is `internal` (driven directly — no
  queue/watchdog/relay, so it's never auto-ended or self-policed), and the guard **can't be enabled
  unless its bot is installed** (`nostall_bot_available`). `bot park` is the deliberate opt-out for a
  single bot.

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

## Multiple installs (N bot processes, N trays) — the OTHER scaling axis
Orthogonal to *Multi-session multiplexing* (one bot process, N Claude sessions): you can also
run **several whole copies** of claudegram, each in its OWN directory with its OWN `token.txt`
(= its own Telegram bot) and its OWN tray window/`bot.py` child. `instance_id.py` (pure, Qt-free,
covered by `test_instance`) is the identity behind it; `gui.py` wraps it in Qt:
- **The fix that enables it:** the tray's single-instance key (`APP_ID`) is now
  `instance_id.instance_key(str(HERE))` — a hash of the install's absolute path, so it's unique
  **per directory**. The old fixed key meant a 2nd copy's `QLocalSocket` probe just poked the 1st
  tray and exited; now a copy elsewhere launches its own tray, and re-launching the SAME copy
  still focuses its own (still one tray per install).
- **Differentiation (zero-config):** `LABEL` = the directory basename with a redundant
  `claudegram-` prefix stripped (`~/claudegram-work` → "work"); the canonical lone `~/claudegram`
  (no `instance.txt`) is `is_default_install` → **left looking exactly as before** (title
  "claudegram", themed mic icon). A named copy gets window title `claudegram · <label>`, a tray
  **badge** (auto color from `accent_hsv(label)` + first-letter `badge_glyph`), badged
  notifications, and a distinct WM class / `.desktop` name (`desktop_name`) so taskbar entries
  don't merge.
- **Override:** optional `instance.txt` (gitignored, per-install) — line 1 = display name, an
  optional `#rrggbb` line = accent color, another line = an explicit badge glyph (e.g. an emoji).
- **Sync point:** `install-autostart.sh` mirrors the label/slug logic in shell; a change to
  `instance_id.py`'s naming must be reflected there (a test compares the two).

## DEPLOY (non-obvious)
- `bot.py` / `claude_driver.py` / `transcribe_worker.py` change → restart **just the bot
  child**: find its PID (`ps -eo pid,cmd | grep '[/]claudegram/bot.py'`) and `kill <pid>`; the
  tray supervisor respawns it and the session resumes (`session.id`). (`transcribe_worker.py` is
  re-read per spawn, so its changes apply on the next voice message even without a restart.)
- `gui.py` / `instance_id.py` change → **full tray restart**: kill the tray + bot child, then
  `./run-gui.sh`. It **self-backgrounds** (re-execs via `setsid`, survives terminal close) and
  gui.py is single-instance **per directory** (see *Multiple installs*). From a non-graphical
  context, pass the desktop env (`WAYLAND_DISPLAY`, `DISPLAY`, `XDG_RUNTIME_DIR`,
  `DBUS_SESSION_BUS_ADDRESS`). Don't do it mid Claude-turn / transcription — wait for idle.
- **Launch paths differ:** manual = `./run-gui.sh` (tray, self-backgrounding) or `./run.sh`
  (headless bot, no tray); **autostart = `./install-autostart.sh`** writes a `.desktop` that
  runs `gui.py` *directly* at login (GNOME-managed) — not `run-gui.sh`. `install-autostart.sh` /
  `uninstall-autostart.sh` name the `.desktop` per-install (mirror of `instance_id.py`), so each
  copy autostarts independently. Keep these in sync.
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
Coverage: `test_multiplex` (registry/routing/badges + the scanned roster), `test_render` (badge on
every message), `test_regressions` (the 2026-07-01 voiceback bugs, pinned), `test_turn` (firewall /
spontaneous relay / crash via mock-Claude), `test_nostall` (anti-stall guard: discovery, flag,
config-driven icon, refuse-without-its-bot), `test_park` (`bot park` + un-park), `test_instance`
(per-install identity: per-dir single-instance key, label/color/glyph, `.desktop` slug). The runner resets
the registry + clears flags between tests.
- **Add a test** whenever you touch bridge logic — it's cheap now. Keep the few *real*-Claude
  checks (isolated `ClaudeController(temp_cwd, temp_session_file, effort='low')`) as a thin
  "contract" layer that pins SDK message shapes + the self-wake-on-shell-complete fact; the
  fakes assume those shapes, so a contract test catches an SDK change the fakes would miss.
- **Never** run a turn against the module-level `controller` — it writes the real `session.id`.

## Logging
Everything (request / thinking / each tool + result / answer / blocks) → `claudegram.log`
(tray "Clear logs" truncates it). Disk is cheap here; log generously.
