# LARGE_ATTACHMENTS.md — big files to/from claudegram

**Status: PROPOSED / UNTESTED design.** Nothing here is implemented yet beyond the
graceful-failure stopgap (see "What exists today"). This file is the plan a
future maintainer (human or bot) can pick up and build. Read it end-to-end
before touching code.

---

## The problem

claudegram talks to Telegram through the **Bot API**. That API has hard size
caps that Telegram enforces server-side and we cannot raise from the client:

- **Download** (a bot fetching a file a user sent it, `getFile`): **20 MB**.
- **Upload** (a bot sending a file to a user, `sendDocument`/`sendAudio`/…): **50 MB**.

Consequences we actually hit:

- A long **voice/audio message** (roughly **> ~15 min**, which is ~20 MB of
  Opus) can't be downloaded. `getFile` raises `BadRequest: File is too big`.
  Before the stopgap this crashed `handle_audio` and left a frozen
  "🎙 Transcribing…" bubble with no feedback.
- **`cg-send`** (delivering a file to the owner's phone) can't send anything
  over 50 MB — same wall, other direction.
- This blocks any "just hand the bot a big file" or "let the bot hand you a big
  file" workflow, which is central to using claudegram for real work
  (recordings, PDFs, exports, media).

Key constraint: **the bot usually runs on a remote/headless machine.** You can't
sit at it and drive a GUI. So "open Telegram Desktop and click download" is not
a general solution — it only works when a human is physically at the machine.

## What exists today (the stopgap, shipped)

`handle_audio` (and the document handler) now **fail gracefully** instead of
crashing:

- A proactive size check: if `file_size > TG_BOT_DL_LIMIT` (20 MB) the bot
  replies with clear guidance ("over Telegram's 20 MB bot limit — split into
  shorter parts and resend, or save via Telegram Desktop") and never starts the
  doomed download / stuck bubble.
- A reactive `try/except` around `get_file`/`download_to_drive` for the case
  Telegram omits `file_size`, editing the bubble to the same guidance and
  releasing the transcription lock cleanly.

That's damage control, not a fix. The user still can't actually get a big file
through. This document is the real fix.

## What does NOT work: a separate "service" Telegram user account

An intuitive idea is a dedicated Telegram **user** account owned by the machine
that "fetches big files." It doesn't work, because of **who can see the file**:

- When a user sends a 20 MB audio to bot *X*, that file lives in the private
  chat **user ↔ bot X**. A separate user account is **not a party to that chat**
  and cannot read it (short of the file being forwarded to it).
- For **sending** big files out, a service user account would send **as itself**
  — the wrong identity; the user expects the file from the bot they talk to.

So a service user account fights the access model in both directions. (A
MTProto user client — Telethon/Pyrogram — is only worth it if you need files
from chats the bots are **not** in, e.g. arbitrary Telegram content. Not our
case.)

## The proposed solution: a local Telegram Bot API server

Telegram publishes the **actual Bot API server** as open source
(`github.com/tdlib/telegram-bot-api`). You can run it **on your own machine**.
When a bot talks to a *local* server instead of `api.telegram.org`:

- The bot keeps **its own identity** — it already has legitimate access to
  exactly the chats where the files are. No second account, no forwarding, no
  reading anyone else's chats.
- Size limits jump from 20/50 MB to **up to 2000 MB (2 GB) for both download
  and upload**. Fixes **both** directions.
- In **local mode**, `getFile` returns a **local absolute file path** — the
  server already has the file on local disk, so there's **no 20 MB HTTP
  download step at all**. The transcriber just reads the path.
- **One daemon serves every bot token on the machine.** All claudegram
  instances point their API base URL at the same local server. This is exactly
  the "one global server on the machine, shared by every instance" model:
  install once per machine, all bots benefit.
- The **end user does nothing.** It's a machine-admin install step.

This is the "first-class, install-once, machine-global large-attachment
capability" — realized as a **bot-API proxy**, not a user account.

### What's required

1. **`api_id` + `api_hash`** — free, one pair per machine, from
   <https://my.telegram.org> → "API development tools". These are **secrets**:
   keep them out of git, in gitignored FILES like the rest of the config
   (`instance.json` fields or a dedicated secret file à la `token.txt` — never
   the environment, per the repo's config doctrine).
2. **The `telegram-bot-api` binary** — build from source (C++/CMake + TDLib) or
   use a prebuilt/Docker image (e.g. `aiogram/telegram-bot-api`).
3. **A place for the daemon to run** as a long-lived service (systemd system or
   user unit), restarted on boot/crash.
4. **A one-time token migration per bot** (see below).
5. **Small bridge changes** to point at the local server and to handle
   local-mode file paths.

### How it would be done

**1. Run the daemon (local mode):**

```
telegram-bot-api \
  --api-id=<API_ID> --api-hash=<API_HASH> \
  --local \
  --http-port=8081 \
  --dir=/var/lib/telegram-bot-api        # where it stores downloaded files
```

`--local` is the important flag: it lifts the limits, enables local file paths,
and lets the bot reference/serve files by path. Wrap this in a systemd unit so
it's always up. Bind it to **localhost only** (never expose 8081 publicly).

**2. Migrate each bot token once.** A token used with the cloud API must be
logged out of it before a local server will accept it:

```
curl https://api.telegram.org/bot<TOKEN>/logOut
```

(To move a bot back to the cloud later, call `logOut` on the **local** server
instead.) Do this once per bot token the machine runs.

**3. Point the bridge at the local server.** In `bot.py`, the
`ApplicationBuilder` currently uses the default cloud URL. Add (python-telegram-bot
v20+ — verify against the installed version):

```python
ApplicationBuilder()
    .token(TOKEN)
    .base_url("http://localhost:8081/bot")
    .base_file_url("http://localhost:8081/file")
    .local_mode(True)
    ...
```

Drive these from config/env so an install **without** the local server keeps the
current cloud behavior untouched:

```
CG_BOT_API_BASE_URL   = http://localhost:8081/bot   # unset -> cloud default
CG_BOT_API_BASE_FILE  = http://localhost:8081/file
CG_BOT_API_LOCAL_MODE = 1
```

**4. Handle local-mode files.** In local mode:
- `get_file()` returns a `File` whose `file_path` is a **local absolute path**.
  For the transcriber, **skip `download_to_drive`** and hand
  `transcribe_worker.py` that path directly. (Clean up per the server's
  retention; the daemon can also auto-delete on read.)
- For **outbound** big files (`cg-send`, media outbox), local mode lets you send
  a **local file path** directly with no 50 MB cap (up to 2 GB).

**5. Make the size gate conditional.** `TG_BOT_DL_LIMIT` (the 20 MB stopgap
threshold) should become **~2000 MB when the local server is active**, so the
graceful "too big" fallback only fires when there is genuinely no local server.
Something like:

```python
TG_BOT_DL_LIMIT = 2000 * 1024 * 1024 if CG_BOT_API_LOCAL_MODE else 20 * 1024 * 1024
```

Keep the stopgap message for the non-local case — it's the correct behavior when
large-attachment support isn't installed.

### Where the code touches (map for the implementer)

- `bot.py` `ApplicationBuilder(...)` — add `base_url` / `base_file_url` /
  `local_mode` from config.
- `bot.py` `TG_BOT_DL_LIMIT` — make it conditional on local mode.
- `bot.py` `handle_audio` — in local mode, use `tg_file.file_path` directly
  instead of `download_to_drive`; feed that path to `transcribe_worker.py`.
- `bot.py` `handle_document` — same local-path handling for PDFs/office files.
- `bot.py` outbound media / `cg-send` path (`media-outbox`) — send local paths;
  the 50 MB cap no longer applies.
- `INSTALL_MANUAL.md` — document the new `instance.json` fields / secret files
  and the install step.

### Testing plan (do this before rollout)

Use **CG2 (bot2) as the guinea pig** — set it up against the local server while
the other instances stay on cloud, so a mistake can't take everyone down.

1. Start the daemon, migrate CG2's token, point CG2's bridge at localhost:8081.
2. **Inbound:** send bot2 a **> 20 MB** (e.g. 30-min) audio → confirm it
   downloads via the local server, transcribes, and replies. Confirm the stopgap
   message does **not** fire.
3. **Outbound:** have bot2 `cg-send` a **> 50 MB** file to the phone → confirm it
   arrives.
4. Confirm an instance **still on cloud** is unaffected.
5. Only then roll the config out to the rest and document it as the optional
   "large attachment support" install step.

### Caveats / risks

- **Token migration is stateful:** a token is bound to one server at a time;
  `logOut` is required to switch. Don't migrate a token you can't afford to take
  offline briefly.
- **Secrets:** `api_id`/`api_hash` and the token must never be committed —
  gitignored secret files only (never env).
- **Disk:** the daemon stores downloaded files under `--dir`. Add retention /
  cleanup so it doesn't grow unbounded (mirror the existing temp-sweep logic).
- **Networking:** bind the daemon to localhost; never expose the API port.
- **Version drift:** pin the `telegram-bot-api` version and re-verify the PTB
  `local_mode` API against the installed `python-telegram-bot`.
- **Not every install wants this:** keep it strictly opt-in; absent the local
  server, the shipped stopgap is the correct behavior.

## Alternatives considered (and why not)

- **MTProto user client (Telethon/Pyrogram):** reliable and no size limit, but
  it can't see the bots' private chats where our files live, and sends under the
  wrong identity. Only useful for pulling arbitrary non-bot content. Rejected
  for this use case.
- **GUI-automating Telegram Desktop:** doesn't work headless/remote, and this
  machine is **Wayland**, where synthetic input into another app's window is
  heavily restricted (no xdotool/wmctrl; needs uinput/portals; brittle).
  Highest effort, lowest reliability. Rejected.
- **Split-and-resend (status quo + stopgap):** works today with zero infra, but
  it's manual and lossy for the user. Fine as the fallback, not the solution.

## References

- Local Bot API server: <https://github.com/tdlib/telegram-bot-api>
- Building/running + local mode: <https://core.telegram.org/bots/api#using-a-local-bot-api-server>
- Get `api_id`/`api_hash`: <https://my.telegram.org>
- python-telegram-bot local mode: `ApplicationBuilder.base_url` /
  `base_file_url` / `local_mode` (verify against installed version).
