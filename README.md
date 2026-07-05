# claudegram

> ## ⚠️ SECURITY — READ THIS FIRST
>
> claudegram drives a **Claude Code instance with `bypassPermissions` (full
> autonomy)**: it can run **any shell command** and read/edit/delete **any file**
> as your user, triggered remotely from your phone. The install-local `work/` is only
> where it *starts* — Bash is **not** a sandbox.
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
what it heard, dispatches the request to a Claude Code instance running in the
install's `work/` directory, and streams back what Claude does plus its answer.

```
phone ──voice/text──▶  your bot  ──▶  transcribe (local) ──▶  Claude Code (work/)
                                                                   │ streams activity
        your bot  ◀── 🗣 echo · 🔧 live board · streamed answer · [[END]] ◀──┘
```

Runs in your system tray, starts at login, locked to your Telegram account only.

> **Installing this for someone?** If you are an AI assistant setting this up for a
> user, follow [`install-manual.md`](install-manual.md) — it walks the secure
> install and lockdown step by step.

## Features

- **Voice → Claude**: voice messages transcribed locally with `faster-whisper`
  (`large-v3`), echoed back, then dispatched. **Text → Claude** too. Transcription runs in a
  **killable subprocess** (`transcribe_worker.py`) watched by a budget watchdog — a
  stalled/looping decode is killed and you're asked to resend, so it can **never freeze the
  bridge**; the bubble shows the real %/ETA and a moving clock. Quality is a **live toggle**:
  `bot transcribe best|good|fast` (float32 / int8_float32 / int8) — no restart.
- **Photo/image → Claude**: send a photo or an image file, with or without a caption, and
  Claude **sees it** (it's multimodal on input) — a screenshot, a diagram, an error on screen.
  No transcription step; the caption (if any) is the instruction, otherwise Claude reads the
  image in the context of your conversation.
- **Voice replies (voiceback)**: **`bot voice on`** makes **every** reply come back as
  **spoken audio** until **`bot voice off`** — a reliable toggle set at the keyboard, not a
  fragile per-message cue. The whole reply is spoken as one voice message, no text transcript.
  (TTS via Kokoro — offline; run `./fetch-kokoro.sh` once to get the model.)
- **Message batching**: fire several messages in a row and the bridge collapses the whole
  queue into **one** Claude turn (combined prompt) instead of answering each separately —
  including anything you send while it's mid-turn (batched into the next one).
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
  of Telegram silence it shows the live state — `🕐 <datetime> · 🟢 working Nm` or `💤 idle`,
  **plus** (orthogonally) the background shells: `🐚 2 shells: build, tests` or `🐚 no shells`.
  It **edits one status message in place** rather than re-posting — same status just bumps a
  `×N` counter and refreshes the leading **datetime** (a moving clock = proof of life); a
  changed status starts a fresh message. So you always know whether to **wait** (idle +
  shells will wake it) or it's **done** (idle + no shells → nothing pending), without spam.
  After ~30 identical **idle + shells** ticks (`×30`, ~30 min), it **auto-nudges Claude**
  to continue, check for stuck shells, or clean them up — so a forgotten shell can't park
  the session forever. And after ~30 identical **idle + no-shells** ticks it **nudges Claude
  to continue or to declare it's done**: if Claude replies starting with `NO MORE WORK` the
  watchdog goes quiet until you send something again — so it can work autonomously through a
  task list without babysitting, yet won't pester you once it's genuinely finished. (While a
  transcription is decoding, the watchdog freezes its counters — that's work, not idleness.)
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
- **Intrusion lock (paranoid tripwire, default ON)**: if **anyone who isn't you** so much as
  messages the bot (text, voice, photo, even `/start`), it's treated as an intrusion —
  **logged**, the bridge is **hard-locked** (Claude killed + `BLOCKED.flag`, physical-unlock
  only), and you get a Telegram **🚨 alert** naming the sender. Toggle it from the **tray
  only** (the 🛡 switch — never a remote command), so the guard can't be disabled over
  Telegram. Heads-up: while on, a stranger who finds the bot can lock you out until you unlock
  at the machine — that's the intended fail-safe.
- **Full logging**: every turn — request, thinking, each tool + result, the full
  answer, compaction, completion, blocks + reasons — is written to `claudegram.log`.
- **Sleep mode**: `bot sleep` pauses **all** Telegram input — Claude keeps running (and
  any background work continues), but messages are ignored and answered with "sleep mode
  engaged, no input accepted". Distinct from `lock` (security) and `kill` (process): the
  only way out is the **WAKE UP** button on the tray app at the machine.
- **Tray app**: live console, auto-restart on crash, single-instance, autostart at
  login, and controls: **Restart bot**, **Unblock**, **Unlock & add regression**, **WAKE UP**
  (exit sleep), **Clear logs**, plus a **🛡 Intrusion Lock** on/off switch on the right
  (green = ON / amber = OFF).

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
| `bot transcribe [best\|good\|fast]` | transcription quality: float32 / int8_float32 / int8 (live, no restart) |
| `bot voice [on\|off]` | spoken replies for **everything** until off |
| `bot drop` | discard messages queued but not yet sent to Claude |
| `bot issues` | list **and clear** recent tool errors (the `⚠️ N issue(s)` detail) |
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
4. **Run the tray app**: `./run-gui.sh` — it **self-backgrounds** (detaches into its own
   session via `setsid`, so closing the terminal won't kill it) and hands the prompt back.
   First run creates the virtualenv, installs deps, and downloads the `large-v3` model
   (~3 GB, once). Re-running is safe (single-instance). Headless, no tray: `./run.sh`.
5. **Autostart at login**: `./install-autostart.sh` (undo: `./uninstall-autostart.sh`). This
   is a **separate path** from the manual launch — at login GNOME runs `gui.py` directly in
   your desktop session; the self-backgrounding `run-gui.sh` is for manual terminal starts.

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | required (or `token.txt`) |
| `ALLOWED_USER_IDS` | — | **required**; comma-separated Telegram ids |
| `CGHOME` | `<install>/work` | Claude's working directory (install-local, gitignored, auto-created) |
| `WHISPER_MODEL` | `large-v3` | transcription model |
| `WHISPER_COMPUTE_TYPE` | `float32` | seeds the default; flip live with `bot transcribe` (good=`int8_float32` ~2×, fast=`int8` ~3-4×) |
| `WHISPER_LANGUAGE` | auto | force e.g. `en` / `pt` |

## Files

| File | Role | Tracked? |
|---|---|---|
| `bot.py` | the bridge: Telegram ↔ transcription ↔ Claude, firewall, intrusion lock, commands, logging | yes |
| `transcribe_worker.py` | standalone, killable whisper decoder (run as a subprocess by `bot.py`) | yes |
| `claude_driver.py` | Claude Agent SDK wrapper (persistent session, subscription enforcement) | yes |
| `gui.py` | tray app: supervises `bot.py`, console, Unblock/Regress/Clear + 🛡 Intrusion Lock toggle | yes |
| `cg-notify` | push a `[HARNESS]` message from this machine to your phone | yes |
| `cg-inbox` | read messages you sent via `bot harness` (`--peek` / `--wait`) | yes |
| `run-harness.sh` | open a visible terminal running a Claude "harness" that operates claudegram | yes |
| `harness-charter.md` | the standing prompt that turns that Claude into the harness | yes |
| `run-gui.sh` / `run.sh` | launchers (`run.sh` runs the bot without the tray) | yes |
| `install-autostart.sh` / `uninstall-autostart.sh` | login autostart | yes |
| `install-manual.md` | step-by-step secure install guide (for an AI assistant) | yes |
| `CLAUDE.md` | dev notes for an AI working on this codebase | yes |
| `HACKING_REGRESSIONS.md` | curated false positives the firewall must never block | yes |
| `.env.example` | template for `.env` | yes |
| `.env`, `token.txt` | **secrets** — never commit | no |
| `session.id`, `effort.level`, `cwd.path`, `compute.type`, `voice.mode`, `BLOCKED.flag`, `SLEEP.flag`, `INTRUSION_OFF.flag` | runtime state | no |
| `outbox/`, `inbox/` | `[HARNESS]` message drop dirs (transient) | no |
| `claudegram.log` | full per-turn transcript (Clear-logs button truncates) | no |

## Harness (optional)

> The bridge is **self-sufficient** — the tray supervises and auto-restarts `bot.py`, the
> watchdog self-heals and nudges, transcription can't wedge it, and Claude extends itself on
> request. So the harness is **genuinely optional**; most installs never run one.

`./run-harness.sh` opens a **visible terminal** running a Claude Code instance pre-prompted
(`harness-charter.md`) to **operate and improve claudegram itself** and to serve your phone
over the `[HARNESS]` channel: it loops on `cg-inbox --wait`, acks every `bot harness` message
via `cg-notify`, and can investigate issues, deploy fixes, and answer meta-questions.

It's deliberately **decoupled and unsupervised**: `bot.py` doesn't know it exists — it's just
"a Claude that understands this install dir", living outside the bridge. Not autostarted; you
run it when you want it. Close the window and it stops (inbox messages safely accumulate until
a harness runs again). Run only **one** at a time. Security posture: it inherits the firewall,
runs with autonomy but **confirms before destructive/security-affecting actions**, and **never
weakens claudegram's own safety controls** (the hard-lock stays physical-unlock only) — and you
can watch it the whole time, which is the point of the visible terminal.

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
