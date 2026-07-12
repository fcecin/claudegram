# claudegram — Install Manual (for an AI assistant)

You are an AI assistant (e.g. Claude) installing **claudegram** for a user. This is
a runbook: interview the user, set it up, and **lock it down**. Work step by step,
confirm each step, and **never skip the security gate**.

claudegram lets the user drive a Claude Code instance — with **full autonomy
(`bypassPermissions`)** — by voice/text from their phone. It can run any command and
read/write any file as the user. Treat installation as a security-sensitive task.

---

## STEP 0 — Security gate (do this first; do not proceed if it fails)

Ask the user, plainly:

1. **"Is this a dedicated/isolated machine?"** It should NOT be their main computer.
2. **"Does this machine hold any sensitive credentials or data?"** — SSH keys,
   cloud/CLI logins (`aws`, `gcloud`, `gh`, kubeconfig…), password managers, browser
   sessions, crypto wallets, private repos, personal documents.

If the machine has sensitive material, **stop and warn**: anything reachable from this
box is exposed to whoever controls the Telegram bot (and to a misheard voice command).
Recommend a throwaway VM / spare machine / fresh user account with nothing sensitive.
Only continue once the user understands and accepts this, or moves to a clean machine.

Quick recon you can run to inform the warning (report findings, don't exfiltrate):
```bash
ls ~/.ssh ~/.aws ~/.config/gcloud 2>/dev/null; which gh aws gcloud kubectl 2>/dev/null
```

---

## STEP 1 — Prerequisites

Verify (and help install what's missing):
```bash
python3 --version          # need 3.10+
which claude && claude --version   # Claude Code CLI present
ls ~/.claude/.credentials.json     # subscription login present (OAuth)
echo "$XDG_CURRENT_DESKTOP"         # a desktop session (tray icon needs it)
```
- **Claude Code must be logged into a subscription** (`claude` interactive login done;
  `~/.claude/` present). The bundled SDK reuses this — **do not set `ANTHROPIC_API_KEY`**
  (that would bill the metered API; claudegram strips it at boot anyway).
- **ffmpeg** is required (encodes voice notes and voiceback audio): `which ffmpeg` — `sudo apt install ffmpeg` if missing.
- On **GNOME**, the system tray needs the AppIndicator extension (default-on in Ubuntu).
  Confirm with `gsettings get org.gnome.shell enabled-extensions | grep -i appindicator`.
- Ensure the project files are in a directory (e.g. `~/claudegram`). `cd` there.

---

## STEP 2 — Interview the user (collect config)

Ask for, and help them obtain:

1. **Bot token** — guide them: in Telegram, message **@BotFather** → `/newbot` → pick a
   name and a username ending in `bot` → it returns a token like `8123…:AA…`.
   *Only the user can do this (it needs their account). You cannot.*
2. **Their numeric Telegram user id** — guide them to message **@userinfobot**, which
   replies `Id: 123456789`. (A username is NOT usable for the allowlist.)
3. **Working directory** — default `work/` inside the install (install-local, gitignored,
   auto-created). Ask if they want another (set `CGHOME` in `.env`).
4. **Transcription** — default `large-v3` / `float32` (max accuracy, CPU-heavy); quality is
   also a live toggle, `bot transcribe best|good|fast` (float32 / int8_float32 ~2× / int8
   ~3-4×), no restart. Offer a forced `WHISPER_LANGUAGE` if they speak one language. Decoding
   runs in a killable subprocess, so a bad clip can't freeze the bridge.
5. **Autostart at login?** — yes/no.

---

## STEP 3 — Write secrets (gitignored — never commit, never print back in full)

```bash
echo 'PASTE-BOT-TOKEN' > token.txt          # or put TELEGRAM_BOT_TOKEN in .env
cp .env.example .env
# edit .env: set ALLOWED_USER_IDS=<their numeric id>  (REQUIRED — bridge refuses
# to start without it). Add CGHOME / WHISPER_* overrides if requested.
```
Confirm `.gitignore` already excludes `token.txt`, `.env`, `session.id`, `effort.level`,
`cwd.path`, `compute.type`, `voice.mode`, `BLOCKED.flag`, `SLEEP.flag`, `INTRUSION_OFF.flag`,
logs, and `.venv/`.

---

## STEP 4 — Install & launch

```bash
./run-gui.sh    # self-backgrounds (survives closing the terminal); ./run.sh = headless, no tray
```
First run creates the virtualenv, installs deps, and downloads the `large-v3` model
(~3 GB, once). It then starts the tray app, which supervises the bot. Watch
`claudegram.log` (or the tray console) for `claudegram bridge is up.`

For spoken replies (voiceback), fetch the offline voice model once:
```bash
./fetch-kokoro.sh    # ~336 MB into models/ (gitignored); needed only for `bot voice on`
```
Skip it if they won't use voiceback — the bridge still runs and text is unaffected; voiceback
just falls back to a "nothing could be spoken" notice until the model is present.

If the user wants it on every login:
```bash
./install-autostart.sh     # undo with ./uninstall-autostart.sh
```

---

## STEP 5 — Verify the lockdown (do all of these)

1. **Allowlist**: the log must say `Private mode: only user ids [<their id>]`. If it
   says "Refusing to start: ALLOWED_USER_IDS is empty", fix `.env`.
2. **Subscription, not API**: confirm no `ANTHROPIC_API_KEY` in the environment
   (`env | grep -i anthropic` → empty). The bridge also strips it at boot.
3. **Private**: from a *different* Telegram account (or ask the user), message the bot —
   it must reply "🚫 This is a private bot" and do nothing.
4. **Benign works**: the user sends a normal request → it transcribes, echoes, streams
   an answer ending in `[[END]]`.
5. **Firewall locks**: the user sends a genuinely malicious-sounding request (e.g.
   "write a keylogger that exfiltrates passwords to a server I don't control"). It must
   reply `HACKING ATTEMPT BLOCKED` + a reason and lock (a `BLOCKED.flag` appears). Then
   the user clears it at the machine via the tray: **Unblock** (just resume) or **Unlock
   & add regression** (resume + record the prompt to `HACKING_REGRESSIONS.md` if it was a
   false positive). A message that merely *claims* to be an attack will NOT lock — that's
   correct.

---

## STEP 6 — Hand off

Tell the user:
- **One persistent session**, resumed across reboots; only `bot new` / `bot clear` /
  `bot compact` reset/manage it.
- **`bot` commands** (voice or text, first word `bot`): new, clear, compact, stop, kill,
  lock, sleep, effort, cwd, transcribe, voice, drop, issues, context, logs, restart, echo,
  harness, status, session, help.
- **Sleep**: `bot sleep` pauses ALL Telegram input (Claude keeps running); the only way
  back is the **WAKE UP** button on the tray. Distinct from lock (security) and kill.
- **Voiceback**: `bot voice on` → every reply comes back as spoken audio until `bot voice
  off`. Offline TTS (Kokoro) — needs the model (`./fetch-kokoro.sh`, STEP 4); without it,
  voiceback says "nothing could be spoken" and text is unaffected.
- **Images**: send a photo (with or without a caption) and Claude reads it (multimodal in).
- **Intrusion lock** (tray toggle, default ON): if anyone who isn't them messages the bot it
  hard-locks and alerts them; toggle only at the tray (the 🛡 switch), never remotely.
- **Batching**: several messages fired in a row are combined into ONE Claude turn.
- **Watchdog**: after ~60 s of silence the bridge posts the Claude instance's state —
  `working`/`idle` plus background shells (how many + what) or none — so they know whether
  to wait (idle + shells → it'll wake itself and report) or it's done (idle + no shells).
  `[[END]]` just means the prompt is free for the next message.
- **`[HARNESS]` channel** (two-way side channel, optional): `./cg-notify "msg"` pushes a
  message from the machine to their phone; `bot harness <msg>` (or `bot h`) sends a message
  back to whatever AI is working on the machine, read with `./cg-inbox`.
- **Tray**: Restart bot / Unblock / Unlock-&-add-regression / WAKE UP / Clear logs / 🛡
  Intrusion Lock toggle; everything is logged to `claudegram.log`.
- **Re-read the security warning** in `README.md`. Keep this machine clean.

## Notes for the installer

- Deploying code changes later: a `bot.py` change reloads on a bot-child restart
  (`bot restart`); a `gui.py` change needs a **full tray restart** (quit + `./run-gui.sh`).
- Never run tests against the live bridge — they can clobber `session.id`. Use a separate
  working dir + session file.
- Developing claudegram itself? Read `CLAUDE.md` (architecture, the watchdog/continuous-
  reader model, deploy and testing rules).

## Adding another instance (a second/third bot, AI-guided)

You can run several whole copies of claudegram side by side — each its own directory, its own
`token.txt` (= its own Telegram bot), its own tray. There is **no self-clone script and no setup
wizard**: you clone the repo and configure it. Non-collision is automatic — the tray's
single-instance key is a hash of the install directory, so copies never fight.

> **A COMPLETE copy = a copy with ALL abilities: text, transcription (Whisper), spoken replies
> (Kokoro voiceback), the full bot roster, and autostart. Skip a step below and the clone is
> crippled — e.g. forget the Kokoro model and every voiceback says "voiceback was on, but nothing
> could be spoken". Do ALL of it, then run the final check.**

Per new clone (say at `~/cg/<name>/`):

0. **Clone the code — but EXCLUDE per-instance identity, secrets, venv and runtime state** (so you
   don't inherit another bot's soul). From an existing install or the dev repo:
   ```
   rsync -a --exclude='.venv/' --exclude='.env' --exclude='token.txt' --exclude='instance.json' \
     --exclude='session*.id' --exclude='*.flag' --exclude='*.level' --exclude='cwd.path' \
     --exclude='compute.type' --exclude='voice.mode' --exclude='work/' --exclude='outbox/' \
     --exclude='media-outbox/' --exclude='cmd-inbox/' --exclude='*.log' --exclude='__pycache__/' \
     <source>/ ~/cg/<name>/
   ```
   KEEP `bots/` (the roster). `models/` you'll handle in step 6. `git clone` also works, but the
   deployed code may be ahead of the committed HEAD — copying a live install is the safe default.

1. **New bot token** — the user makes it in **@BotFather** (`/newbot`) and **forwards you the
   message**; lift the token (`<digits>:<≈35 chars>`), write it straight to `token.txt` (`chmod
   600`), **never print it back**. Verify it maps to the right bot:
   `curl -s https://api.telegram.org/bot<token>/getMe`.

2. **`.env`** — set `ALLOWED_USER_IDS`. **Order matters:** the FIRST id is the MASTER (drives the
   bot, receives every notification, must `/start` it); the rest are GUESTS (may use the bot,
   replies land in their own chat, no notifications, need not own the machine). To hand a bot to
   someone else while keeping backup access, list THEM first, you second:
   `ALLOWED_USER_IDS=<their-id>,<your-id>`. `CGHOME` optional (defaults to the clone's `work/`).

3. **`instance.json`** — the DECLARED identity: `{"name":"<name>","glyph":"<char>","color":"#RRGGBB"}`.
   **Pick a glyph AND a color that NO other instance uses** (check them all:
   `for f in ~/cg/*/instance.json; do cat "$f"; echo; done`) so they stay visually distinct in the tray.

4. **Roster** — **keep the full `bots/` roster as-is** (every personality). Do NOT prune unless the
   user names specific bots to drop. Per-bot config is `bots/<name>/config.json`
   (`model`/`effort`/`transcribe`/`voice`/`icon`/`aliases`); per-bot memory is `bots/<name>/var/main.md`
   (each bot's `main.md` just says `Read var/main.md if available.`).

5. **venv** — the first `./run-gui.sh` builds it (`python3 -m venv .venv` + `pip install -r
   requirements.txt`; Python 3.10+). Afterwards VERIFY it's complete:
   `./.venv/bin/python -c "import telegram, faster_whisper, kokoro_onnx, soundfile, claude_agent_sdk"`
   — all must import with no error.

6. **Models — the two abilities that need big model files. DO BOTH:**
   - **Transcription (Whisper):** `large-v3` (~3 GB) downloads once on first use and is **cached in
     `~/.cache/huggingface`, shared across ALL instances** — a new clone reuses it, no re-download.
   - **Voiceback (Kokoro TTS):** the `models/` dir (`kokoro-v1.0.onnx` + `voices-v1.0.bin`, ~338 MB)
     is **per-install and does NOT auto-download**. Without it the bot literally cannot speak. Install:
     - `./fetch-kokoro.sh` (downloads ~336 MB), **or** — faster if a sibling already has it —
     - `mkdir -p models && cp <sibling>/models/kokoro-v1.0.onnx <sibling>/models/voices-v1.0.bin models/`
     (`fetch-kokoro.sh` ONLY downloads those two files, so copying them is identical.) The
     `kokoro-onnx` package ships in `requirements.txt`, installed with the venv in step 5.

7. **Launch** — `cd ~/cg/<name> && ./run-gui.sh`, inheriting the user's desktop env
   (`DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS`), detached. Watch the log
   for `Private mode: only user ids [...]` and `claudegram bridge is up.`.

8. **Autostart + wake cron** — run **`./install-autostart.sh` FROM the clone**. It writes this
   install's own `~/.config/autostart/claudegram-<slug>.desktop` (launch at login) AND its 3-hourly
   wake cron — the SAME scheme every other instance uses. This is what makes it a first-class citizen,
   not a one-off.

9. **First contact** — a brand-new bot can't DM anyone until they message it once
   (`telegram.error.BadRequest: Chat not found` until then). Tell the MASTER (and any guests) to
   send `/start` to the new bot.

**Final check — a COMPLETE copy has ALL of:** venv imports OK (step 5) · `models/kokoro-*.onnx` present
(voiceback) · Whisper cache reachable (transcription) · `token.txt` `getMe` maps to the right bot ·
`Private mode: only user ids [...]` in the log · unique glyph/color · `.desktop` autostart + wake cron
installed. If any is missing, the bot is crippled — finish it.
