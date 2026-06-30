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
3. **Working directory** — default `~/cghome` (auto-created). Ask if they want another.
4. **Transcription** — default `large-v3` / `float32` (max accuracy, CPU-heavy). Offer
   `WHISPER_COMPUTE_TYPE=int8` (~3–4× faster, slight accuracy loss) and a forced
   `WHISPER_LANGUAGE` if they speak one language.
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
`cwd.path`, `BLOCKED.flag`, logs, and `.venv/`.

---

## STEP 4 — Install & launch

```bash
./run-gui.sh
```
First run creates the virtualenv, installs deps, and downloads the `large-v3` model
(~3 GB, once). It then starts the tray app, which supervises the bot. Watch
`claudegram.log` (or the tray console) for `claudegram bridge is up.`

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
  lock, sleep, effort, cwd, context, logs, restart, echo, harness, status, session, help.
- **Sleep**: `bot sleep` pauses ALL Telegram input (Claude keeps running); the only way
  back is the **WAKE UP** button on the tray. Distinct from lock (security) and kill.
- **Watchdog**: after ~60 s of silence the bridge posts the Claude instance's state —
  `working`/`idle` plus background shells (how many + what) or none — so they know whether
  to wait (idle + shells → it'll wake itself and report) or it's done (idle + no shells).
  `[[END]]` just means the prompt is free for the next message.
- **`[HARNESS]` channel** (two-way side channel, optional): `./cg-notify "msg"` pushes a
  message from the machine to their phone; `bot harness <msg>` (or `bot h`) sends a message
  back to whatever AI is working on the machine, read with `./cg-inbox`.
- **Tray**: Unblock / Unlock-&-add-regression / WAKE UP / Restart bot / Clear logs;
  everything is logged to `claudegram.log`.
- **Re-read the security warning** in `README.md`. Keep this machine clean.

## Notes for the installer

- Deploying code changes later: a `bot.py` change reloads on a bot-child restart
  (`bot restart`); a `gui.py` change needs a **full tray restart** (quit + `./run-gui.sh`).
- Never run tests against the live bridge — they can clobber `session.id`. Use a separate
  working dir + session file.
- Developing claudegram itself? Read `CLAUDE.md` (architecture, the watchdog/continuous-
  reader model, deploy and testing rules).
