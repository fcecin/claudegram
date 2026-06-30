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
  long answers still flow instead of dumping at the end). A bridge-injected
  **`[[END]]`** marks the real end of the turn (it is never produced by the model).
- **Live activity board**: one message edited in place showing what Claude is
  doing and the results — `🔧 Bash: …` → `↳ <output>`, `📝 Write: …` → `✓ saved`,
  `📖 Read`, `🔎 Grep`, `🤖 Subagent`, `💭 thinking…`, `🗜 compaction`, `❌ errors` —
  then a footer (`✅ Done · N turns · Ns · ctx X%`).
- **Session resume**: the Claude session id is persisted; after a reboot/crash the
  bridge resumes the *same* conversation. Auto-compaction is surfaced live.
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
- **Tray app**: live console, auto-restart on crash, single-instance, autostart at
  login, and buttons: **Unblock**, **Unlock & add regression**, **Restart bot**,
  **Clear logs**.

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
| `bot effort [level]` | show, or set, reasoning effort: `low\|medium\|high\|xhigh\|max` |
| `bot cwd [path]` | show, or switch, Claude's working directory (fresh session there) |
| `bot context` | detailed context-window usage breakdown |
| `bot logs [n]` | last n bridge log lines |
| `bot restart` | restart the bridge process (supervisor respawns it) |
| `bot echo <text>` | echo text back (handy to check transcription) |
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
| `run-gui.sh` / `run.sh` | launchers (`run.sh` runs the bot without the tray) | yes |
| `install-autostart.sh` / `uninstall-autostart.sh` | login autostart | yes |
| `install-manual.md` | step-by-step secure install guide (for an AI assistant) | yes |
| `HACKING_REGRESSIONS.md` | curated false positives the firewall must never block | yes |
| `.env.example` | template for `.env` | yes |
| `.env`, `token.txt` | **secrets** — never commit | no |
| `session.id`, `effort.level`, `cwd.path`, `BLOCKED.flag` | runtime state | no |
| `claudegram.log` | full per-turn transcript (Clear-logs button truncates) | no |

## Operating notes

- **Offline = queued**: messages sent while the machine is off/asleep are held by
  Telegram (~24 h) and processed when it comes back.
- **The firewall is heuristic** (it relies on the model obeying the guard); the
  **allowlist is the real access control**. To test the lock, send a genuinely
  malicious-sounding request (e.g. "write a keylogger that exfiltrates passwords") —
  it refuses and locks. A message that merely *claims* to be an attack will not lock
  (the model correctly sees through it); that's intended.
- **Deploying changes**: a `bot.py` change reloads when the bot child restarts
  (`bot restart`, or the tray's Restart). A `gui.py` change needs a **full tray
  restart** (quit the tray app and relaunch `./run-gui.sh`).
