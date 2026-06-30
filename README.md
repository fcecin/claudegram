# claudegram

> ## ⚠️ SECURITY — READ THIS FIRST
>
> claudegram drives a **Claude Code instance with `bypassPermissions` (full
> autonomy)**: it can run **any shell command** and read/edit/delete **any file**
> as your user, triggered remotely from your phone. `~/cghome` is only where it
> *starts* — Bash is **not** a sandbox.
>
> **Run claudegram only on a dedicated, isolated machine that holds NO sensitive
> credentials or data** — no SSH keys, cloud/CLI logins (AWS/GCP/gh/etc.), password
> managers, browser sessions, crypto wallets, private repos, or personal documents.
> Assume anything reachable from that machine is exposed to whoever controls the
> Telegram bot (and to a stray voice command).
>
> The account allowlist and the hacking-firewall reduce risk but are **guardrails,
> not a sandbox**. Do not run this on your main workstation.

A private Telegram bridge that lets you **drive a Claude Code instance by voice or
text from your phone**. You talk to your bot; it transcribes locally, echoes back
what it heard, dispatches the request to a Claude Code instance running in
`~/cghome`, and streams back what Claude does plus its answer.

```
phone ──voice/text──▶  your bot  ──▶  transcribe (local) ──▶  Claude Code (~/cghome)
                                                                   │ streams activity
        your bot  ◀── 🗣 echo · 🔧 live board · streamed answer · [[END]] ◀──┘
```

Runs in your system tray, starts at login, locked to your Telegram account only.

> **Installing this for someone?** If you are an AI assistant setting this up for a
> user, follow [`install-manual.md`](install-manual.md) — it walks the secure
> install and lockdown step by step.

## Features

- **Voice → Claude**: voice messages transcribed locally with `faster-whisper`
  (`large-v3`, full precision), echoed back, then dispatched. **Text → Claude** too.
- **Streaming answers**: the answer arrives as paragraph messages as Claude writes
  them, with a ~3 s coalescing window (adjacent paragraphs batch into one message;
  long answers still flow instead of dumping at the end). Clean paragraph breaks are
  preserved even when a tool call splits the prose. A bridge-injected **`[[END]]`**
  marks that the turn is done and **the prompt is free for your next message** (it is
  never produced by the model).
- **Live activity board**: one message edited in place showing what Claude is
  doing and the results — `🔧 Bash: …` → `↳ <output>`, `📝 Write: …` → `✓ saved`,
  `📖 Read`, `🔎 Grep`, `🤖 Subagent`, `💭 thinking…`, `🗜 compaction`, `❌ errors`. The
  board **freezes once the answer starts** streaming below it (so an older bubble never
  mutates above the answer), and the `✅ Done · N turns · Ns · ctx X%` summary lands as
  a new message at the bottom.
- **Watchdog (never silent)**: a continuous monitor of the Claude instance. After ~60 s
  of Telegram silence it posts the live state — `🟢 working Nm` or `💤 idle`, **plus**
  (orthogonally) the background shells: `🐚 2 shells: build, tests` or `🐚 no shells`.
  So you always know whether to **wait** (idle + shells will wake it) or it's **done**
  (idle + no shells → nothing pending). No more staring at a dead-looking screen.
- **Reports its own background work**: when Claude finishes a turn with a background
  shell still running and then **wakes itself** as the task lands, that follow-up turn is
  relayed to your phone too (`🔔 Claude picked back up…`) — not just lost on the machine.
- **One conversation, always**: the session id is persisted and resumed after a
  reboot/crash; `bot cwd` even **migrates** the conversation across directories so the
  same thread follows you everywhere (only `bot new`/`clear` resets it). The
  conversation id is surfaced in the activity board, the footer, and `bot status`.
  Auto-compaction is surfaced live.
- **Lifecycle pings**: it sends you a Telegram **🟢 online** message (with session,
  cwd, model, effort) whenever the bridge starts — so you see power-cycles from your
  phone — and logs a **🛑 exited** line with signal + uptime on shutdown.
- **`[HARNESS]` two-way channel**: a side channel between your phone and any program on
  the machine (e.g. an AI working there). **Machine → phone**: `./cg-notify "msg"` pushes
  a `🤖 [HARNESS]` message to you ("build's green"). **Phone → machine**: `bot harness
  <msg>` (or `bot h <msg>`) drops a message into `inbox/` for the AI to read with
  `./cg-inbox` (`--wait` blocks until one arrives) — so you can talk back to it.
- **"bot" commands**: any message whose first word is `bot` is a harness command,
  never sent to Claude (see below). Slash equivalents `/new /stop /status` also work.
- **Your subscription, not the API**: strips `ANTHROPIC_API_KEY` & friends at boot
  so the instance uses your OAuth subscription (no separate metered billing).
- **Locked to you**: refuses to start without an allowlist; serves only your
  Telegram user id; everyone else is refused before any work happens.
- **Hacking firewall (hard lock)**: a small guard tells Claude to refuse a genuine
  malicious hacking/intrusion attempt by replying `HACKING ATTEMPT BLOCKED` + the
  reason. The bridge detects that, writes a persistent `BLOCKED.flag`, interrupts,
  and refuses all further work until someone clears it **at the machine**. False
  positives are recorded to `HACKING_REGRESSIONS.md` so they don't recur.
- **Full logging**: every turn — request, thinking, each tool + result, the full
  answer, compaction, completion, blocks + reasons — is written to `claudegram.log`.
- **Sleep mode**: `bot sleep` pauses **all** Telegram input — Claude keeps running (and
  any background work continues), but messages are ignored and answered with "sleep mode
  engaged, no input accepted". Distinct from `lock` (security) and `kill` (process): the
  only way out is the **WAKE UP** button on the tray app at the machine.
- **Tray app**: live console, auto-restart on crash, single-instance, autostart at
  login, and buttons: **Unblock**, **Unlock & add regression**, **WAKE UP** (exit sleep),
  **Restart bot**, **Clear logs**.

## "bot" commands

A message (voice or text) whose **first word is `bot`** is handled by the bridge
itself and is **never sent to Claude**. Unknown ones reply
`[claudegram] "bot" command unknown: …`.

| Command | Effect |
|---|---|
| `bot new` / `bot clear` | fresh conversation (clears Claude's context) |
| `bot compact` | compact the conversation (summarize to free context) |
| `bot stop` | interrupt the current task (Esc/Ctrl-C — graceful) |
| `bot kill` | `kill -9` the Claude process; it respawns (resuming the session) |
| `bot lock` | `bot kill` **and** lock the bridge — unlock only at the machine |
| `bot sleep` | pause **all Telegram input** (Claude keeps running); wake only at the machine |
| `bot effort [level]` | show, or set, reasoning effort: `low\|medium\|high\|xhigh\|max` |
| `bot cwd [path]` | show, or switch, Claude's working directory (the conversation **migrates** with you — same id) |
| `bot context` | detailed context-window usage breakdown |
| `bot logs [n]` | last n bridge log lines |
| `bot restart` | restart the bridge process (supervisor respawns it) |
| `bot echo <text>` | echo text back (handy to check transcription) |
| `bot harness <text>` / `bot h <text>` | message the AI/harness working on this machine (lands in `inbox/`) |
| `bot status` | bridge state · effort · session id · context % · lock state |
| `bot session` | current session id |
| `bot help` | list these |

## Requirements

- A **dedicated, isolated Linux machine** (see the security warning) with a desktop
  session for the tray icon (GNOME needs the AppIndicator extension, on by default
  on Ubuntu).
- **Python 3.10+** and **ffmpeg/PyAV** (pulled in by the deps).
- The **Claude Code CLI logged into a subscription** (`claude` — `~/.claude/`
  present). The bundled SDK CLI reuses that login; no API key is set.

## Setup

1. **Create the bot**: in Telegram, message **@BotFather** → `/newbot` → pick a
   name and a username ending in `bot` → it gives you a **token**.
2. **Token**: `echo 'YOUR-TOKEN' > token.txt` (gitignored). Or put it in `.env`.
3. **Lock it to you**: find your numeric Telegram id via **@userinfobot**, then set
   `ALLOWED_USER_IDS=<id>` in `.env` (copy `.env.example`). The bridge refuses to
   start without this.
4. **Run the tray app**: `./run-gui.sh` — first run creates the virtualenv,
   installs deps, and downloads the `large-v3` model (~3 GB, once).
5. **Autostart at login**: `./install-autostart.sh` (undo: `./uninstall-autostart.sh`).

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | required (or `token.txt`) |
| `ALLOWED_USER_IDS` | — | **required**; comma-separated Telegram ids |
| `CGHOME` | `~/cghome` | Claude's working directory (auto-created) |
| `WHISPER_MODEL` | `large-v3` | transcription model |
| `WHISPER_COMPUTE_TYPE` | `float32` | `int8` = ~3-4× faster, slight accuracy loss |
| `WHISPER_LANGUAGE` | auto | force e.g. `en` / `pt` |

## Files

| File | Role | Tracked? |
|---|---|---|
| `bot.py` | the bridge: Telegram ↔ transcription ↔ Claude, firewall, commands, logging | yes |
| `claude_driver.py` | Claude Agent SDK wrapper (persistent session, subscription enforcement) | yes |
| `gui.py` | tray app: supervises `bot.py`, console, Unblock/Regress/Clear buttons | yes |
| `cg-notify` | push a `[HARNESS]` message from this machine to your phone | yes |
| `cg-inbox` | read messages you sent via `bot harness` (`--peek` / `--wait`) | yes |
| `run-gui.sh` / `run.sh` | launchers (`run.sh` runs the bot without the tray) | yes |
| `install-autostart.sh` / `uninstall-autostart.sh` | login autostart | yes |
| `install-manual.md` | step-by-step secure install guide (for an AI assistant) | yes |
| `CLAUDE.md` | dev notes for an AI working on this codebase | yes |
| `HACKING_REGRESSIONS.md` | curated false positives the firewall must never block | yes |
| `.env.example` | template for `.env` | yes |
| `.env`, `token.txt` | **secrets** — never commit | no |
| `session.id`, `effort.level`, `cwd.path`, `BLOCKED.flag`, `SLEEP.flag` | runtime state | no |
| `outbox/`, `inbox/` | `[HARNESS]` message drop dirs (transient) | no |
| `claudegram.log` | full per-turn transcript (Clear-logs button truncates) | no |

## Operating notes

- **Offline = queued**: messages sent while the machine is off/asleep are held by
  Telegram (~24 h) and processed when it comes back.
- **Background work & the watchdog**: if Claude kicks off a long task in the background,
  its turn ends (you get `[[END]]`) but the work keeps running. The watchdog's 60 s pulse
  tells you what's up: **idle + shells** → wait, Claude will wake itself and report when
  the task lands; **idle + no shells** → it's truly done, nothing pending (if it promised
  to "get back to you" with no shells, that won't happen — just message it again).
- **The firewall is heuristic** (it relies on the model obeying the guard); the
  **allowlist is the real access control**. To test the lock, send a genuinely
  malicious-sounding request (e.g. "write a keylogger that exfiltrates passwords") —
  it refuses and locks. A message that merely *claims* to be an attack will not lock
  (the model correctly sees through it); that's intended.
- **Deploying changes**: a `bot.py` change reloads when the bot child restarts
  (`bot restart`, or the tray's Restart). A `gui.py` change needs a **full tray
  restart** (quit the tray app and relaunch `./run-gui.sh`).
