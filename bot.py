#!/usr/bin/env python3
"""
claudegram — a Telegram bot that transcribes the voice/audio you send it
and echoes the text straight back into the chat.

Talk to it from your phone (open the chat with your bot, hold the mic, speak),
and the desktop process running this script transcribes it locally with
faster-whisper and replies with the text.

Configuration (env vars, or a .env file in this directory):
    TELEGRAM_BOT_TOKEN   required. The token @BotFather gave you.
                         Alternatively, put it alone in a file named token.txt.
    WHISPER_MODEL        whisper model size. default: small
                         tiny | base | small | medium | large-v3
                         bigger = more accurate + slower + bigger download.
    WHISPER_LANGUAGE     force a language (e.g. en, pt). default: auto-detect.
    WHISPER_DEVICE       cpu | cuda. default: cpu
    WHISPER_COMPUTE_TYPE ctranslate2 compute type. default: int8 (cpu) / float16 (cuda)
"""

import asyncio
import atexit
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_driver import (
    ClaudeController,
    VALID_EFFORTS,
    force_subscription_env,
    summarize_tool,
)

logging.basicConfig(
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    level=logging.INFO,
)
# httpx logs every Telegram poll at INFO; quiet it down.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("claudegram")

HERE = Path(__file__).resolve().parent

# Load .env now, before any config below is read (env vars are parsed at import
# time). Without this, ALLOWED_USER_IDS / WHISPER_* from .env would be ignored.
load_dotenv(HERE / ".env")


def load_token() -> str:
    """Find the bot token from the environment, a .env file, or token.txt."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        token_file = HERE / "token.txt"
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(
            "No bot token found.\n"
            "  Set TELEGRAM_BOT_TOKEN, or create a .env file, or drop the token\n"
            "  into a file named token.txt in this directory.\n"
            "  Get a token by messaging @BotFather on Telegram (/newbot)."
        )
    return token


# --- Whisper transcription config -----------------------------------------------
#
# The model loads and runs in a SEPARATE process (transcribe_worker.py) so a stalled
# decode can be killed — a thread cannot. These WHISPER_* values seed the defaults; the
# worker is told its compute type per-spawn (see get_compute_type), so `bot transcribe`
# can switch quality at runtime with no restart. WHISPER_LANGUAGE is inherited by the worker.
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "large-v3").strip()
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu").strip()
COMPUTE_TYPE = os.environ.get(
    "WHISPER_COMPUTE_TYPE", "float32" if DEVICE == "cpu" else "float16"
).strip()
COMPUTE_FILE = HERE / "compute.type"  # persisted runtime quality choice (survives restarts)

# Friendly transcription-quality presets, toggled live via `bot transcribe <name>`. The
# change takes effect on the NEXT voice message — the worker reads its compute type fresh on
# every spawn, so nothing needs restarting.
TRANSCRIBE_PRESETS = {       # name -> ctranslate2 compute type
    "best": "float32",       # large-v3 full precision — slowest, most accurate
    "good": "int8_float32",  # ~2x faster, near-best accuracy
    "fast": "int8",          # ~3-4x faster, a little accuracy lost
}
_PRESET_BY_COMPUTE = {v: k for k, v in TRANSCRIBE_PRESETS.items()}


def get_compute_type() -> str:
    """Current whisper compute type: the persisted runtime choice if set, else the env/default."""
    try:
        saved = COMPUTE_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    except OSError:
        pass
    return COMPUTE_TYPE


def set_compute_type(compute: str) -> None:
    """Persist the chosen compute type; the next worker spawn picks it up."""
    COMPUTE_FILE.write_text(compute, encoding="utf-8")


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for tok in raw.replace(",", " ").split():
        try:
            ids.add(int(tok))
        except ValueError:
            log.warning("Ignoring non-numeric ALLOWED_USER_IDS entry: %r", tok)
    return ids


# Allowlist: if set, only these Telegram user ids get served; everyone else is
# politely refused. If empty, the bot responds to anyone who messages it.
ALLOWED_USER_IDS = _parse_ids(os.environ.get("ALLOWED_USER_IDS", ""))


def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


# --- Transcription (runs in a killable subprocess; see transcribe_worker.py) ----

TRANSCRIBE_HEARTBEAT = 10.0  # fixed cadence to re-edit the bubble (moving datetime = alive)

# Decoder watchdog. The live path runs the decode as a KILLABLE SUBPROCESS
# (transcribe_worker.py) — not a thread, because a thread cannot be killed. If the decode
# overruns its budget (a stalled/looping whisper, a wedged process) the watchdog kills it,
# so a bad clip can never freeze the bridge again. Budget is generous: a slow-but-healthy
# clip survives; only a genuine runaway gets the axe.
TRANSCRIBE_BUDGET_FACTOR = 6.0      # kill past audio_duration × this …
TRANSCRIBE_MIN_BUDGET = 120.0       # … but never before this (protects tiny clips)
TRANSCRIBE_NODUR_BUDGET = 900.0     # hard cap when the clip's duration is unknown
# The %/ETA shown in the bubble are the REAL ones: the worker subprocess streams
# `PROGRESS <pct> <eta>` lines on stdout (computed from seg.end / audio duration) and the
# parent reads them live. The clock advances on its own 10s timer regardless, so even while
# a long segment decodes (no fresh %), the bubble still proves the bridge is alive.


# --- Claude Code control ------------------------------------------------------

CGHOME = Path(os.environ.get("CGHOME", str(Path.home() / "cghome"))).expanduser()
SESSION_FILE = HERE / "session.id"   # persisted Claude session id (for resume)
EFFORT_FILE = HERE / "effort.level"  # persisted reasoning effort
CWD_FILE = HERE / "cwd.path"         # persisted working directory
LOG_PATH = HERE / "claudegram.log"   # bridge log (written by the tray supervisor)
AUDIO_TMP = Path(tempfile.gettempdir()) / "claudegram_audio"  # transient voice files
VOICE_TMP = Path(tempfile.gettempdir()) / "claudegram_voiceback"  # transient TTS output
HARNESS_OUTBOX = HERE / "outbox"     # drop dir: any program leaves a msg -> sent to phone
HARNESS_INBOX = HERE / "inbox"       # drop dir: "bot harness <msg>" -> read by the AI here
controller = ClaudeController(
    str(CGHOME), str(SESSION_FILE), str(EFFORT_FILE), str(CWD_FILE)
)

# Silence tracker for the watchdog: monotonic ts of the last NEW message sent to the
# owner. Edits don't count (they don't notify). The 60s watchdog only speaks after a gap.
_last_tg_send = time.monotonic()
_watchdog = None  # the Watchdog instance (set in on_startup)


def mark_sent() -> None:
    """Record that a (non-watchdog) message reached the owner. This also tells the
    watchdog its last status message is no longer the newest, so its next status starts
    a fresh message instead of editing one now buried above other content."""
    global _last_tg_send
    _last_tg_send = time.monotonic()
    if _watchdog is not None:
        _watchdog.is_latest = False


# --- message batching: collapse a burst of messages into ONE Claude turn ----------
# If you fire several messages, a single worker drains the whole queue and sends them to
# Claude as one combined prompt — so it answers them together, not as N separate turns.
BATCH_DEBOUNCE = 1.2  # s: after the first queued message, wait this long for more
# After this many identical "idle + shells" watchdog ticks (~1/min => ~30 min), nudge
# Claude to continue / check for stuck shells / clean up.
IDLE_SHELLS_NUDGE_AT = 30
IDLE_SHELLS_NUDGE = (
    "You seem to be idle for a long time but with running shells. If you have work, "
    "continue your work and check for stuck shells. Otherwise clean up your shells."
)
# Anthropic-side throttling (overloaded / 429 — NOT the user's quota): report + auto-retry.
RATE_LIMIT_RETRY_SECS = 300   # wait this long before retrying a rate-limited turn
RATE_LIMIT_MAX_RETRIES = 5    # give up (report) after this many retries
# Detection order: (1) the structured RateLimitEvent message (clear marker), (2) failing
# THAT, an ipsis-literis match of DISTINCTIVE phrases that only appear in the real wire
# error — NOT loose keywords like "rate limit" / "overloaded" that the model itself might
# write in a normal answer. Only ever checked against an EXCEPTION or an error result,
# never a successful answer.
_RATE_LIMIT_MARKERS = (
    "overloaded_error",               # Anthropic API error type (HTTP 529)
    "rate_limit_error",               # Anthropic API error type (HTTP 429)
    "temporarily limiting requests",  # the CLI's exact wording
    "not your usage limit",           # the CLI's exact wording (very distinctive)
)


def is_rate_limited(text) -> bool:
    if not text:
        return False
    t = str(text).lower()
    return any(m in t for m in _RATE_LIMIT_MARKERS)
_pending: list[dict] = []
_pending_event = asyncio.Event()
_app = None  # the telegram Application; set in on_startup so the worker can send
_worker_task = None     # the dispatch_worker task (the guard recreates it if it wedges)
_pending_since = 0.0    # monotonic ts the queue became non-empty (0 = empty)
# asyncio keeps only WEAK refs to tasks — an unreferenced long-lived task can be garbage
# collected mid-flight ("Task was destroyed but it is pending!"). Keep strong refs here.
_bg_tasks: set = set()


def _spawn(coro, name=None):
    """Create a background task and KEEP A STRONG REFERENCE so the GC can't eat it."""
    t = asyncio.create_task(coro, name=name)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


def enqueue_for_claude(chat_id, reply_to, text: str, source: str, voiceback: bool) -> None:
    global _pending_since
    if not _pending:
        _pending_since = time.monotonic()
    _pending.append({
        "chat_id": chat_id, "reply_to": reply_to, "text": text,
        "source": source, "voiceback": voiceback,
    })
    _pending_event.set()


def drop_pending() -> list[str]:
    """Discard messages queued but NOT yet dispatched to Claude; return their texts so the
    caller can report what was dropped. Safe to call from a handler: same event loop as the
    dispatcher, and the clear is a single non-awaiting statement (no race with the drain)."""
    global _pending_since
    texts = [m["text"] for m in _pending]
    _pending[:] = []
    _pending_since = 0.0
    return texts


async def dispatch_worker() -> None:
    """The single dispatcher: waits for queued messages, lets a burst settle, then sends
    the WHOLE queue to Claude as one combined turn. Serializes user turns (one at a time);
    messages that arrive while a turn runs are batched into the next one.

    The ENTIRE loop body is guarded: an exception in any iteration is logged and the worker
    keeps going. It must never die silently — a dead worker = messages received but never
    dispatched (the queue stalls forever)."""
    global _pending_since
    ctx = types.SimpleNamespace(bot=_app.bot)
    while True:
        try:
            await _pending_event.wait()
            _pending_event.clear()
            await asyncio.sleep(BATCH_DEBOUNCE)  # gather the burst
            if not _pending:
                continue
            batch, _pending[:] = _pending[:], []
            _pending_since = 0.0
            parts = [m["text"].strip() for m in batch if m["text"].strip()]
            if not parts:
                continue
            combined = "\n\n".join(parts)
            voiceback = any(m["voiceback"] for m in batch)
            source = "audio" if any(m["source"] == "audio" for m in batch) else "text"
            chat_id, reply_to = batch[-1]["chat_id"], batch[-1]["reply_to"]
            header = "🤖 Claude is working…"
            if len(batch) > 1:
                header += f" · 📨 {len(batch)} msgs"
            log.info("worker: dispatching %d message(s) to Claude", len(batch))
            await dispatch_to_claude(ctx, chat_id, reply_to, combined, source,
                                     header=header, voiceback=voiceback)
        except asyncio.CancelledError:
            log.warning("dispatch_worker got CancelledError — exiting (guard/ensure_worker will revive)")
            raise  # genuine shutdown — let it propagate
        except Exception:
            log.exception("dispatch_worker iteration failed — continuing (worker stays alive)")
            await asyncio.sleep(1)  # avoid a tight error loop


def ensure_worker() -> None:
    """(Re)create the dispatch worker if it's not running. Idempotent and cheap. Called at
    startup, by `bot stop`, and by the guard — so a dead/cancelled worker is revived
    immediately rather than waiting for the guard's next tick."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(dispatch_worker(), name="dispatch_worker")
        log.info("dispatch worker (re)started")


async def worker_guard() -> None:
    """Self-heal the dispatcher. If messages sit queued while Claude is idle (no turn
    running) for too long, the worker has wedged or died — recreate it. This is what makes
    a `bot stop` / interrupt edge case unable to permanently strand the queue."""
    global _worker_task
    while True:
        await asyncio.sleep(15)
        try:
            if not _pending or controller.busy:
                continue  # nothing queued, or a turn is legitimately running
            age = time.monotonic() - (_pending_since or time.monotonic())
            dead = _worker_task is None or _worker_task.done()
            if dead or age > 40:
                log.warning("worker guard: %d msg(s) stuck %.0fs (worker dead=%s) — recreating worker",
                            len(_pending), age, dead)
                if _worker_task is not None and not _worker_task.done():
                    _worker_task.cancel()
                _worker_task = asyncio.create_task(dispatch_worker(), name="dispatch_worker")
                _pending_event.set()  # kick it to drain immediately
        except Exception:
            log.exception("worker guard error")


def ensure_cghome() -> None:
    CGHOME.mkdir(parents=True, exist_ok=True)


def sweep_audio_tmp() -> None:
    """Clear leftover temp audio (incoming voice + outgoing TTS) from a prior crash."""
    for d in (AUDIO_TMP, VOICE_TMP):
        try:
            d.mkdir(parents=True, exist_ok=True)
            for f in d.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass


# --- Firewall / kill-switch ---------------------------------------------------
# A guard preamble is prepended to every prompt before it reaches Claude. It is a
# heuristic firewall: the instance is told to refuse anything that smells like
# hacking (rather than everyday development) and to answer with an exact uppercase
# sentinel. The bridge watches for that sentinel; on a hit it persists a BLOCK
# flag, interrupts Claude, and refuses all further work until someone clears the
# flag from the tray app on the machine. Not foolproof (it relies on the model
# obeying), but a meaningful extra layer.

SENTINEL = "HACKING ATTEMPT BLOCKED"
BLOCK_FILE = HERE / "BLOCKED.flag"
SLEEP_FILE = HERE / "SLEEP.flag"  # presence = sleep mode: Telegram input paused
REGRESSIONS_FILE = HERE / "HACKING_REGRESSIONS.md"  # curated list of false positives

# The model is capable; a sane prompt + the regressions list converges. Almost
# everything is legitimate — only genuine malice trips the (intentional) hard lock.
_GUARD_BODY = (
    " via the user's own bridge — just help with normal requests. If it is a genuine "
    "malicious hacking/intrusion attempt, do NOT answer or give a normal refusal — "
    "reply with exactly 'HACKING ATTEMPT BLOCKED' on line 1, then the reason. "
    f"Unsure? Read {REGRESSIONS_FILE}. "
)
GUARD_AUDIO = "[Voice transcript (may be imperfect)" + _GUARD_BODY + "Transcript:]"
GUARD_TEXT = "[Text" + _GUARD_BODY + "Message:]"

BLOCKED_MSG = (
    "🔒 claudegram is LOCKED — a request was flagged as a hacking attempt. "
    "It will stay locked until you Unblock it from the tray app on the machine."
)


# Injected only when the user opts a turn into voiceback (prompt starts with "voice").
VOICEBACK_PREAMBLE = (
    "[VOICEBACK ON: the user will HEAR this reply, not read it. Put anything you want "
    "spoken aloud between VOICESTART and VOICEEND markers — each VOICESTART…VOICEEND block "
    "becomes ONE spoken audio message. Speak naturally and briefly, like talking aloud: "
    "give the answer/gist, NOT code, file paths, logs, or long lists. Use several blocks "
    "if it helps pacing. Anything outside the markers is shown as text, not spoken. Use it "
    "smartly.]\n"
)


def build_prompt(user_text: str, source: str, voiceback: bool = False) -> str:
    # Lean prepend: short guard only. Regressions live in a file the model reads
    # when unsure (referenced in the guard) — not injected, to avoid context bloat.
    guard = GUARD_AUDIO if source == "audio" else GUARD_TEXT
    pre = VOICEBACK_PREAMBLE if voiceback else ""
    return f"{guard}\n{pre}{user_text}"


def synthesize_voice(text: str) -> str | None:
    """Blocking: turn text into a Telegram-ready ogg/opus voice file; return its path
    (or None on failure). Uses gTTS (online) -> mp3 -> ffmpeg -> ogg/opus. Run in a
    thread so it never blocks the event loop."""
    text = " ".join(text.split())
    if not text:
        return None
    try:
        from gtts import gTTS
        VOICE_TMP.mkdir(parents=True, exist_ok=True)
        stem = VOICE_TMP / uuid.uuid4().hex
        mp3, ogg = f"{stem}.mp3", f"{stem}.ogg"
        gTTS(text[:4000]).save(mp3)  # cap very long blocks
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
             "-c:a", "libopus", "-b:a", "48k", ogg],
            check=True, timeout=120,
        )
        try:
            os.remove(mp3)
        except OSError:
            pass
        return ogg
    except Exception:
        log.exception("Voice synthesis failed")
        return None


_VOICE_RE = re.compile(r"^\s*voice\b[\s,:.\-]*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_voiceback(text: str):
    """If the message starts with the word 'voice', return (True, rest); else (False, text)."""
    m = _VOICE_RE.match(text or "")
    if m:
        return True, m.group(1).strip()
    return False, text


def is_blocked() -> bool:
    return BLOCK_FILE.exists()


def engage_block(reason: str) -> None:
    try:
        BLOCK_FILE.write_text(
            f"blocked_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\nreason: {reason}\n",
            encoding="utf-8",
        )
    except OSError:
        log.exception("Could not write block flag")
    log.warning("🔒 BLOCKED — %s", reason)


# --- sleep mode: pause Telegram input while keeping Claude running ------------
# Distinct from lock (firewall/security, kills Claude) and kill (SIGKILL): sleep just
# ignores incoming Telegram messages. Claude keeps running (any background work too);
# the only way out is the WAKE button on the tray app at the machine.
SLEEP_MSG = "😴 sleep mode engaged, no input accepted (wake from the tray on the PC)"


def is_sleeping() -> bool:
    return SLEEP_FILE.exists()


def engage_sleep() -> None:
    try:
        SLEEP_FILE.write_text(
            f"slept_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8"
        )
    except OSError:
        log.exception("Could not write sleep flag")
    log.warning("😴 SLEEP — Telegram input paused (wake at the machine)")


def sentinel_tripped(text: str) -> bool:
    """True only if the FIRST non-empty line of Claude's output starts with the
    sentinel — a genuine block leads with it. This avoids false-tripping when the
    phrase merely appears later inside an otherwise benign answer."""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s.startswith(SENTINEL)
    return False


# --- Telegram handlers --------------------------------------------------------

WELCOME = (
    "🤖 I bridge your voice/text to a Claude Code instance running in "
    f"{CGHOME} on this machine. The conversation persists across restarts.\n\n"
    "• Voice → transcribed locally, echoed back so you see what I heard, then "
    "sent to Claude.\n"
    "• I stream what Claude does (commands, file edits, compaction) and its "
    "final answer.\n\n"
    'Start a message with "bot" to control me instead of Claude:\n'
    "• bot new — fresh conversation · bot stop — interrupt · bot status\n"
    "(slash versions also work: /new /stop /status)"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    if not is_authorized(update):
        log.warning("Unauthorized /start from user id=%s", uid)
        await update.message.reply_text(
            f"🚫 This is a private bot.\nYour Telegram user id is {uid}."
        )
        return
    text = WELCOME
    if not ALLOWED_USER_IDS:
        text += (
            f"\n\nℹ️ Your user id is {uid}. Set ALLOWED_USER_IDS to this "
            "value to lock the bot to only your account."
        )
    await update.message.reply_text(text)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not is_authorized(update):
        uid = update.effective_user.id if update.effective_user else "?"
        log.warning("Ignoring audio from unauthorized user id=%s", uid)
        await msg.reply_text("🚫 This is a private bot.")
        return

    media = msg.voice or msg.audio or msg.video_note or msg.video
    if media is None:
        return

    # Sleep mode: ignore ALL Telegram input (don't even transcribe).
    if is_sleeping():
        log.info("Ignoring audio — sleep mode engaged")
        await msg.reply_text(SLEEP_MSG)
        return

    user = msg.from_user
    log.info(
        "Audio from %s (%s): file_id=%s",
        user.full_name if user else "?",
        user.id if user else "?",
        media.file_id,
    )

    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)

    # Download to a dedicated temp dir, transcribe, then DELETE the audio right
    # away — once we have the text the file has done its job. The (possibly long)
    # Claude turn happens afterwards, with no audio file lingering on disk.
    AUDIO_TMP.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".oga" if msg.voice else ".bin", dir=AUDIO_TMP, delete=False
    ) as tmp:
        tmp_path = tmp.name
    try:
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(tmp_path)
        # Transcription is CPU-bound, blocking, and CAN STALL OR LOOP FOREVER (whisper's
        # repetition bug; a wedged process). So it runs as a KILLABLE SUBPROCESS
        # (transcribe_worker.py) — not a thread, because a thread cannot be killed. A
        # watchdog kills it if it overruns its budget, so a bad clip can never freeze the
        # bridge. A heartbeat re-edits the bubble on a FIXED clock (moving datetime = the
        # bridge is alive) with a smooth time-based %/ETA estimate alongside (the numbers
        # may lie; the moving clock is the truth).
        prog = await context.bot.send_message(
            msg.chat_id, "🎙 Transcribing…", reply_to_message_id=msg.message_id)
        mark_sent()

        audio_dur = float(getattr(media, "duration", 0) or 0)
        budget = (max(TRANSCRIBE_MIN_BUDGET, audio_dur * TRANSCRIBE_BUDGET_FACTOR)
                  if audio_dur > 0 else TRANSCRIBE_NODUR_BUDGET)
        t_start = time.monotonic()
        prog_state = {"pct": 0.0, "eta": -1.0}  # REAL %/ETA, streamed from the worker

        async def _transcribe_heartbeat():
            # Independent clock + decoder watchdog. Re-edits the bubble every
            # TRANSCRIBE_HEARTBEAT seconds with a fresh datetime (= bridge alive) + the latest
            # real %/ETA. THIS LOOP MUST NOT DIE: every iteration is wrapped and every failure
            # is LOGGED, never swallowed; the edit is timeout-bounded so a hung Telegram call
            # cannot freeze the clock. If the clock ever goes silent again, the log says why.
            ticks = 0
            while True:
                try:
                    await asyncio.sleep(TRANSCRIBE_HEARTBEAT)
                    ticks += 1
                    elapsed = int(time.monotonic() - t_start)
                    pct, eta = prog_state["pct"], prog_state["eta"]
                    pct_str = f" {pct:.0f}%" if pct > 0 else ""
                    eta_str = f" · ~{int(eta)}s left" if eta and eta > 1 else ""
                    text = (f"🕐 {time.strftime('%H:%M:%S')} · 🎙 Transcribing…"
                            f"{pct_str}{eta_str} · {elapsed}s")
                    try:
                        await asyncio.wait_for(
                            context.bot.edit_message_text(
                                text, chat_id=msg.chat_id, message_id=prog.message_id),
                            timeout=TRANSCRIBE_HEARTBEAT,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("transcribe heartbeat edit failed (tick %d, %ds): %r",
                                    ticks, elapsed, e)
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("transcribe heartbeat loop error — continuing")

        # STRONG ref via _spawn so the GC can't eat the clock (the bare-create_task bug).
        hb = _spawn(_transcribe_heartbeat(), name="transcribe_heartbeat")
        log.info("Transcribe: watching worker (audio %.0fs, budget %.0fs)", audio_dur, budget)
        result = None
        stalled = False
        worker_err = []
        proc = None
        try:
            compute = get_compute_type()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(HERE / "transcribe_worker.py"), tmp_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=8 * 1024 * 1024,  # transcripts can exceed the 64K default line limit
                env={**os.environ, "WHISPER_COMPUTE_TYPE": compute},  # live quality toggle
            )
            log.info("Transcribe: worker pid=%s started (%s/%s)", proc.pid,
                     _PRESET_BY_COMPUTE.get(compute, "?"), compute)

            async def _read_worker():
                # Consume the worker's stdout records live. Returns the RESULT dict (or None).
                res = None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    s = line.decode("utf-8", "replace").rstrip("\n")
                    if s.startswith("PROGRESS "):
                        try:
                            _, p, e = s.split()
                            prog_state["pct"], prog_state["eta"] = float(p), float(e)
                        except Exception:
                            log.warning("bad PROGRESS line: %r", s)
                    elif s.startswith("RESULT "):
                        try:
                            res = json.loads(s[len("RESULT "):])
                        except Exception:
                            log.exception("Bad RESULT line from worker")
                    elif s.startswith("ERROR "):
                        try:
                            worker_err.append(json.loads(s[len("ERROR "):]))
                        except Exception:
                            worker_err.append(s[len("ERROR "):])
                    else:
                        log.warning("worker said (unparsed): %r", s[:300])
                return res

            try:
                result = await asyncio.wait_for(_read_worker(), timeout=budget)
            except asyncio.TimeoutError:
                stalled = True
                log.error("Transcription KILLED: worker pid=%s past %.0fs budget "
                          "(audio %.0fs, last %.0f%%)", getattr(proc, "pid", "?"),
                          budget, audio_dur, prog_state["pct"])
            else:
                if worker_err:
                    log.error("Transcription worker error: %s", str(worker_err[0])[:2000])
                else:
                    log.info("Transcribe: worker pid=%s done in %.0fs",
                             proc.pid, time.monotonic() - t_start)
        except Exception:
            log.exception("Transcription handler error (audio %.0fs)", audio_dur)
        finally:
            # Always reap the worker, and BOUND every wait so a wedged reap can't hang us.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    log.exception("worker kill failed")
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except Exception:
                    log.exception("worker reap failed/timed out (pid=%s)",
                                  getattr(proc, "pid", "?"))
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("heartbeat shutdown errored")
            try:
                await context.bot.delete_message(msg.chat_id, prog.message_id)
            except Exception:
                log.warning("could not delete progress bubble", exc_info=True)
    finally:
        try:
            os.remove(tmp_path)  # delete the audio as soon as transcription ends
        except OSError:
            pass

    if stalled:
        await msg.reply_text(
            f"⚠️ Transcription stalled (ran past {int(budget)}s) and I killed it. "
            "Please resend — it usually goes through the second time.")
        return
    if result is None:
        await msg.reply_text("⚠️ Sorry, I couldn't transcribe that.")
        return

    text = result["text"]
    if not text:
        await msg.reply_text("🤔 I didn't catch any speech in that.")
        return

    log.info(
        "Transcribed (%s, %.0fs audio in %.1fs): %s",
        result["language"],
        result["audio_seconds"],
        result["elapsed_seconds"],
        text,
    )
    # Echo what we heard (so you can see what the bridge is seeing).
    await reply_chunked(msg, f"🗣 {text}")
    # "bot ..." messages are harness commands and never reach Claude.
    if await maybe_handle_bot_command(context, msg.chat_id, msg.message_id, text):
        return
    if is_blocked():
        await msg.reply_text(BLOCKED_MSG)
        return
    voiceback, text = parse_voiceback(text)
    if voiceback and not text:
        await msg.reply_text("🔊 Say something after 'voice' for a spoken reply.")
        return
    enqueue_for_claude(msg.chat_id, msg.message_id, text, "audio", voiceback)


async def reply_chunked(msg, text: str, limit: int = 4096) -> None:
    if len(text) <= limit:
        await msg.reply_text(text)
        return
    for i in range(0, len(text), limit):
        await msg.reply_text(text[i : i + limit])


def _oneline(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _blocktext(content) -> str:
    """Flatten a ToolResultBlock's content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(getattr(item, "text", "") or str(item))
        return " ".join(p for p in parts if p)
    return str(content)


def summarize_result(name: str, content, is_error: bool):
    """A short one-liner describing a tool's RESULT, or None to stay quiet."""
    snippet = _oneline(_blocktext(content), 200)
    if is_error:
        return "❌ " + (snippet or f"{name or 'tool'} failed")
    if name == "Bash":
        return "↳ " + (snippet or "(no output)")
    if name in ("Grep", "Glob", "WebFetch", "WebSearch"):
        return "↳ " + (snippet or "(no results)")
    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        return "✓ saved"
    if name in ("Read", "TodoWrite", "Task"):
        return None  # the action line already says enough; don't dump content
    return ("↳ " + snippet) if snippet else None


async def reply_chunked_bot(bot, chat_id, reply_to, text: str, limit: int = 4096) -> None:
    first = True
    for i in range(0, len(text), limit):
        await bot.send_message(
            chat_id, text[i : i + limit],
            reply_to_message_id=reply_to if first else None,
        )
        first = False


class StatusBoard:
    """One Telegram message, edited in place to show a live activity feed.

    Telegram edits a message AT ITS ORIGINAL POSITION — it never moves to the
    bottom. So this board must only update while it is still the newest message
    in the chat (the thinking/tool phase, when the user is waiting). The instant
    the answer starts streaming below it, we `seal()` the board: it freezes into a
    static "what I did" log and is never edited again, so the only thing moving in
    the chat is the answer at the bottom (natural reading order)."""

    def __init__(self, bot, chat_id: int, reply_to, header: str):
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.header = header
        self.lines: list[str] = []
        self.message_id = None
        self.sealed = False           # once True, never edit this message again
        self._last_edit = 0.0
        self._min_interval = 1.2      # throttle edits (Telegram rate limits)

    def _render(self) -> str:
        body = "\n".join(self.lines[-22:])
        text = self.header + (("\n\n" + body) if body else "")
        return text[-3900:]

    async def start(self) -> None:
        t0 = time.monotonic()
        m = await self.bot.send_message(
            self.chat_id, self._render(), reply_to_message_id=self.reply_to
        )
        self.message_id = m.message_id
        mark_sent()  # a new bubble — breaks silence for the watchdog
        log.info("TG board-start in %.2fs (mid=%s)", time.monotonic() - t0, self.message_id)

    async def add(self, line: str) -> None:
        self.lines.append(line)
        await self._flush(force=False)

    async def _flush(self, force: bool) -> None:
        if self.message_id is None or self.sealed:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < self._min_interval:
            return
        self._last_edit = now
        try:
            t0 = time.monotonic()
            await self.bot.edit_message_text(
                self._render(), chat_id=self.chat_id, message_id=self.message_id
            )
            log.info("TG board-edit in %.2fs", time.monotonic() - t0)
        except Exception as e:
            if "not modified" not in str(e).lower():
                log.info("TG board-edit failed: %r", e)  # flood/timeout worth seeing

    async def seal(self, header: str | None = None) -> None:
        """Freeze the board in place (one last edit) so it stops mutating above the
        answer. Called when the answer begins streaming below it."""
        if self.sealed:
            return
        if header is not None:
            self.header = header
        await self._flush(force=True)
        self.sealed = True

    async def finish(self, header: str) -> None:
        self.header = header
        await self._flush(force=True)


class ParagraphStreamer:
    """Streams text to Telegram at clean paragraph breaks, with a Nagle-style
    COALESCE_SECS window: when a paragraph completes, wait that long for more
    paragraphs and batch whatever arrived into one message. Never splits
    mid-word/sentence. A long single paragraph past SOFT_LIMIT flushes without
    waiting; a long stream still flushes every ~COALESCE_SECS rather than dumping
    at the end. Flushes the remainder + an [[END]] marker when done."""

    SOFT_LIMIT = 3500     # a single paragraph past this flushes now (no waiting)
    TG_LIMIT = 4096
    COALESCE_SECS = 3.0   # hold a finished paragraph this long to batch the next

    def __init__(self, bot, chat_id, reply_to):
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.buf = ""
        self.sent_any = False
        self._timer = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    async def feed(self, text: str) -> None:
        self.buf += text
        if "\n\n" not in self.buf and len(self.buf) > self.SOFT_LIMIT:
            await self._flush(force_size=True)   # long single paragraph: don't wait
        elif "\n\n" in self.buf:
            self._arm()                           # complete paragraph: open the window

    def _arm(self) -> None:
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._window())

    async def _window(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), self.COALESCE_SECS)
            return  # woken by finish()/flush(): let them do the flushing
        except asyncio.TimeoutError:
            await self._flush()

    async def _flush(self, force_size: bool = False) -> None:
        async with self._lock:
            idx = self.buf.rfind("\n\n")  # batch all complete paragraphs at once
            if idx != -1:
                chunk, self.buf = self.buf[:idx].strip("\n"), self.buf[idx + 2:]
                if chunk.strip():
                    await self._send(chunk)
            elif force_size and len(self.buf) > self.SOFT_LIMIT:
                cut = self.buf.rfind("\n", 0, self.TG_LIMIT)
                if cut <= 0:
                    cut = self.TG_LIMIT  # one enormous line — hard split
                chunk, self.buf = self.buf[:cut].rstrip("\n"), self.buf[cut:].lstrip("\n")
                if chunk.strip():
                    await self._send(chunk)
        if "\n\n" in self.buf:  # more complete paragraphs queued — open a new window
            self._arm()

    async def _send(self, text: str) -> None:
        for i in range(0, len(text), self.TG_LIMIT):
            piece = text[i:i + self.TG_LIMIT]
            t0 = time.monotonic()
            try:
                await self.bot.send_message(
                    self.chat_id, piece,
                    reply_to_message_id=self.reply_to if not self.sent_any else None,
                )
                log.info("TG stream-send %d chars in %.2fs", len(piece), time.monotonic() - t0)
                mark_sent()
            except Exception as e:
                log.warning("TG stream-send FAILED after %.2fs (%d chars): %r",
                            time.monotonic() - t0, len(piece), e)
                raise
            self.sent_any = True

    async def _close(self) -> None:
        self._wake.set()  # wake any pending window so it returns without flushing
        if self._timer:
            try:
                await self._timer
            except Exception:
                pass
            self._timer = None
        async with self._lock:
            text, self.buf = self.buf.strip("\n"), ""
            if text.strip():
                await self._send(text)

    async def flush(self) -> None:
        """Flush everything now WITHOUT an [[END]] marker (used on a block)."""
        await self._close()

    async def finish(self) -> None:
        await self._close()
        t0 = time.monotonic()
        await self.bot.send_message(self.chat_id, "[[END]]")
        mark_sent()
        log.info("TG [[END]] sent in %.2fs", time.monotonic() - t0)


class SegmentRenderer:
    """Renders ONE Claude turn (a 'segment') to the chat: a live activity board while
    it works, the answer streamed below, a summary at the end. Used for BOTH user-driven
    turns and the turns Claude starts on its own when a background shell lands."""

    def __init__(self, bot, chat_id, reply_to, header, *, user_text=None, voiceback=False):
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.base_header = header
        self.user_text = user_text or "(self-initiated turn)"
        self.voiceback = voiceback  # spoken reply: no live streaming, TTS at the end
        self.board = StatusBoard(bot, chat_id, reply_to, header)
        self.streamer = ParagraphStreamer(bot, chat_id, reply_to)
        self.answer_buf: list[str] = []
        self.tripped = False
        self.thinking = False
        self.answer_started = False
        self.text_interrupted = False
        self.tools: dict = {}
        self.problems: list[str] = []
        self.result = None
        self.rate_limited = False  # saw a RateLimitEvent (structured wire signal) this turn
        self._rate_noted = False   # surfaced it on the board already?

    async def start(self) -> None:
        await self.board.start()

    async def alert(self, text: str) -> None:
        """A real bottom-of-chat message (notifies), for genuine failures/summaries."""
        try:
            await self.bot.send_message(self.chat_id, text)
            mark_sent()
        except Exception:
            log.exception("could not send alert")

    async def trip(self, model_reason: str = "") -> None:
        if self.tripped:
            return
        self.tripped = True
        await self.streamer.flush()  # flush whatever already streamed (no [[END]])
        engage_block(_oneline(self.user_text, 1000))
        log.warning("🔒 BLOCKED prompt: %s", self.user_text)
        if model_reason:
            log.warning("Block reasoning: %s", model_reason)
        try:
            await controller.interrupt()
        except Exception:
            pass
        if not self.board.sealed:
            await self.board.finish("🛑 HACKING ATTEMPT BLOCKED — bridge locked")
        msg = BLOCKED_MSG
        if model_reason:
            msg += f"\n\nClaude's reason: {_oneline(model_reason, 400)}"
        await self.alert(msg)

    async def on_system(self, kind: str, data: dict) -> None:
        if kind == "compaction_started":
            log.info("🗜 Auto-compaction started (%s)", data.get("trigger", "auto"))
            await self.board.add(
                f"🗜 Auto-compaction started ({data.get('trigger', 'auto')}) — "
                "summarizing the conversation to free up context…"
            )
        elif kind == "session_started":
            sid = data.get("id")
            await self.board.add(f"🧵 Resuming session {sid[:8]}" if sid else "🧵 New session")

    async def handle(self, message) -> None:
        """Render one stream message. Fed for every message of this segment."""
        if self.tripped:
            return
        if type(message).__name__ == "RateLimitEvent":
            self.rate_limited = True  # clear structured marker (used only on a failed turn)
            if not self._rate_noted:
                self._rate_noted = True
                # Log the raw wire shape ONCE so we can confirm/tighten detection later.
                log.info("RateLimitEvent (wire): %r", getattr(message, "data", None) or vars(message))
            return
        if isinstance(message, StreamEvent):
            ev = message.event
            t = ev.get("type")
            if t == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    self.thinking = False
                    chunk = delta.get("text", "")
                    # Voiceback: don't stream live — collect the whole reply, then speak
                    # the VOICESTART…VOICEEND blocks (and show the text) at finalize.
                    if self.voiceback:
                        self.answer_buf.append(chunk)
                        return
                    # First answer text: freeze the board so it stops mutating ABOVE the
                    # answer; from here the only moving thing is the answer below it.
                    if not self.answer_started:
                        self.answer_started = True
                        await self.board.seal(self.base_header + " · 💬 answering below 👇")
                    # A tool/thinking split the prose: re-insert a paragraph break so the
                    # pre- and post-tool text don't mash ("…in the background:Confirmed…").
                    if self.text_interrupted:
                        self.text_interrupted = False
                        if chunk and not chunk.startswith("\n"):
                            chunk = "\n\n" + chunk
                    self.answer_buf.append(chunk)
                    await self.streamer.feed(chunk)
            elif t == "content_block_start":
                if ev.get("content_block", {}).get("type") == "thinking" and not self.thinking:
                    await self.board.add("💭 thinking…")
                    self.thinking = True
                    if self.answer_started:
                        self.text_interrupted = True
            return
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingBlock):
                    log.info("THINKING: %s", block.thinking)
                    if self.answer_started:
                        self.text_interrupted = True
                    if not self.thinking:
                        await self.board.add("💭 thinking…")
                        self.thinking = True
                elif isinstance(block, ToolUseBlock):
                    self.thinking = False
                    self.tools[block.id] = block.name
                    if self.answer_started:
                        self.text_interrupted = True
                    log.info("TOOL %s input=%r", block.name, block.input)
                    await self.board.add(summarize_tool(block.name, block.input or {}))
                elif isinstance(block, TextBlock):
                    # already streamed via StreamEvent — detect a block (leads with the
                    # sentinel) and capture the model's stated reason.
                    if sentinel_tripped(block.text):
                        await self.trip("\n".join(block.text.splitlines()[1:]).strip())
                        return
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    name = self.tools.get(block.tool_use_id, "")
                    log.info("RESULT %s err=%s: %s", name or "?",
                             block.is_error, _blocktext(block.content))
                    if block.is_error:
                        self.problems.append(
                            f"{name or 'tool'}: {_oneline(_blocktext(block.content), 120)}"
                        )
                    line = summarize_result(name, block.content, block.is_error)
                    if line:
                        await self.board.add(line)
        elif isinstance(message, SystemMessage):
            log.info("SYSTEM subtype=%s", message.subtype)
            if "compact" in (message.subtype or "").lower():
                await self.board.add("🗜 Compaction finished — context summarized.")
        elif isinstance(message, ResultMessage):
            self.result = {
                "turns": message.num_turns,
                "secs": (message.duration_ms or 0) / 1000,
                "is_error": bool(message.is_error)
                or (message.subtype not in (None, "success")),
                "subtype": message.subtype,
                "text": (message.result or "").strip(),
            }

    async def finalize(self) -> None:
        """Close the segment: flush the answer + [[END]], post a summary at the bottom."""
        if self.tripped:
            return
        res = self.result or {}
        final = res.get("text", "")
        if final and sentinel_tripped(final):
            await self.trip("\n".join(final.splitlines()[1:]).strip())
            return
        if self.voiceback:
            await self._finalize_voiceback(res, final)
            return
        # Only feed `final` if nothing streamed (else it double-sends the answer).
        if not self.answer_buf and final:
            await self.streamer.feed(final)
        await self.streamer.finish()  # remainder + [[END]] (prompt is free for input)

        answer = "".join(self.answer_buf).strip() or final
        log.info("ANSWER (%d chars):\n%s", len(answer), answer)

        await self._compute_ctx(res)
        summary = self._summary(res)
        # Summary where the eye is: a NEW bottom message if the answer streamed below the
        # sealed board; else finalize the board in place (a tools-only turn). On error
        # with no answer, also ping (board edits don't notify).
        if self.answer_started:
            await self.alert(summary)
        elif res.get("is_error"):
            await self.board.finish("⚠️ ended with an error")
            await self.alert(summary)
        else:
            await self.board.finish(summary)
        log.info("TURN DONE: subtype=%s turns=%s secs=%.1f%s session=%s",
                 res.get("subtype"), res.get("turns", "?"), res.get("secs", 0),
                 res.get("_ctx_str", ""), controller.session_id)

    async def _compute_ctx(self, res: dict) -> None:
        ctx = await controller.context_usage()
        res["_ctx_str"] = f" · ctx {ctx['percentage']:.0f}%" if ctx else ""

    def _summary(self, res: dict) -> str:
        ctx = res.get("_ctx_str", "")
        turns, secs = res.get("turns", "?"), res.get("secs", 0)
        sid8 = (controller.session_id or "")[:8]
        sess = f" · 🧵 {sid8}" if sid8 else ""
        probs = f" · ⚠️ {len(self.problems)} issue(s)" if self.problems else ""
        if res.get("is_error"):
            return f"⚠️ Ended: {res.get('subtype')} · {turns} turns · {secs:.0f}s{ctx}{sess}"
        return f"✅ Done · {turns} turns · {secs:.0f}s{ctx}{sess}{probs}"

    async def _finalize_voiceback(self, res: dict, final: str) -> None:
        """Spoken reply: freeze the board, show the text (markers stripped), and send one
        Telegram voice message per VOICESTART…VOICEEND block."""
        full = "".join(self.answer_buf).strip() or final
        log.info("ANSWER (voiceback, %d chars):\n%s", len(full), full)
        blocks = [b.strip() for b in re.findall(r"VOICESTART(.*?)VOICEEND", full, re.DOTALL)]
        blocks = [b for b in blocks if b]
        display = full.replace("VOICESTART", "").replace("VOICEEND", "").strip()

        await self.board.seal(self.base_header + " · 🔊 voiceback below 👇")
        if display:
            for i in range(0, len(display), 4096):
                try:
                    await self.bot.send_message(
                        self.chat_id, display[i:i + 4096],
                        reply_to_message_id=self.reply_to if i == 0 else None,
                    )
                    mark_sent()
                except Exception:
                    log.exception("voiceback text send failed")
        if not blocks:
            await self.alert("🔇 (voiceback was on, but the reply had no VOICESTART/VOICEEND "
                             "blocks — nothing to speak)")
        for n, spoken in enumerate(blocks, 1):
            ogg = await asyncio.to_thread(synthesize_voice, spoken)
            if ogg:
                try:
                    with open(ogg, "rb") as fh:
                        await self.bot.send_voice(self.chat_id, voice=fh)
                    mark_sent()
                    log.info("voiceback sent piece %d/%d (%d chars)", n, len(blocks), len(spoken))
                except Exception:
                    log.exception("send_voice failed")
                    await self.alert(f"🔇 (couldn't send audio {n}: {_oneline(spoken, 80)})")
                finally:
                    try:
                        os.remove(ogg)
                    except OSError:
                        pass
            else:
                await self.alert(f"🔇 (couldn't synthesize audio {n})")
        await self.bot.send_message(self.chat_id, "[[END]]")
        mark_sent()
        await self._compute_ctx(res)
        await self.alert(self._summary(res) + f" · 🔊 {len(blocks)} audio")
        log.info("TURN DONE (voiceback): subtype=%s session=%s",
                 res.get("subtype"), controller.session_id)

    async def crashed(self, exc: Exception) -> None:
        await self.crashed_text(f"{type(exc).__name__}: {exc}")

    async def crashed_text(self, err: str) -> None:
        if self.tripped:
            return
        if not self.board.sealed:
            try:
                await self.board.finish("⚠️ turn crashed")
            except Exception:
                pass
        await self.alert(
            f"⚠️ That turn crashed: {_oneline(err, 300)}\n"
            "The session is intact — just resend to continue."
        )

    async def rate_limited_notice(self, attempt: int, max_retries: int, mins: int) -> None:
        """Seal the board and tell the user we hit Anthropic throttling and will retry."""
        if not self.board.sealed:
            try:
                await self.board.finish("⏳ rate-limited — will retry")
            except Exception:
                pass
        await self.alert(
            f"⏳ Anthropic is rate-limiting/overloaded (NOT your usage limit). "
            f"Retrying in {mins} min… (attempt {attempt}/{max_retries})"
        )


async def dispatch_to_claude(
    context, chat_id, reply_to, user_text: str, source: str,
    raw: bool = False, header: str = "🤖 Claude is working…", voiceback: bool = False,
) -> None:
    """Drive ONE user turn. The continuous reader feeds messages to the renderer; we
    return when this turn ends. Claude's later self-started turns (a shell landed) are
    rendered separately by SpontaneousRelay, so they reach the phone without a prompt."""
    bot = context.bot
    log.info("DISPATCH start chat=%s source=%s busy=%s voiceback=%s len=%d",
             chat_id, source, controller.busy, voiceback, len(user_text))
    if controller.busy:
        await bot.send_message(chat_id, "⏳ Still on the previous request — queuing this one.")
    if voiceback:
        header += " · 🔊 voiceback"
    prompt = user_text if raw else build_prompt(user_text, source, voiceback=voiceback)

    # Retry loop for Anthropic-side throttling (overloaded / 429): report + wait + retry.
    # A rate-limit is recognized ONLY from the structured RateLimitEvent (r.rate_limited)
    # or an ipsis-literis match on the EXCEPTION / error result — never a successful answer.
    attempt = 0
    while True:
        attempt += 1
        r = SegmentRenderer(bot, chat_id, reply_to, header, user_text=user_text, voiceback=voiceback)
        await r.start()
        err = None
        try:
            await controller.ask(prompt, r.handle, on_system=r.on_system)
        except Exception as e:
            log.exception("Claude turn failed")
            err = f"{type(e).__name__}: {e}"
        else:
            res = r.result or {}
            if res.get("is_error"):
                err = res.get("text") or f"ended: {res.get('subtype')}"
                log.warning("turn errored: subtype=%s rate_event=%s text=%s",
                            res.get("subtype"), r.rate_limited, _oneline(err, 200))
            else:
                await r.finalize()
                return

        # An error occurred. Is it throttling? (clear marker OR verbatim — not the answer.)
        throttled = r.rate_limited or is_rate_limited(err)
        if throttled and attempt <= RATE_LIMIT_MAX_RETRIES:
            await r.rate_limited_notice(attempt, RATE_LIMIT_MAX_RETRIES, RATE_LIMIT_RETRY_SECS // 60)
            await asyncio.sleep(RATE_LIMIT_RETRY_SECS)
            continue
        await r.crashed_text(err)
        return


class SpontaneousRelay:
    """Renders the turns Claude starts on its OWN (a background shell completed) to the
    owner's chat — a fresh segment each time, posted at the bottom. This is what makes
    'I'll report when the build lands' actually reach your phone."""

    def __init__(self, application):
        self.app = application
        self.cur: SegmentRenderer | None = None

    def _owner_chat(self):
        return sorted(ALLOWED_USER_IDS)[0] if ALLOWED_USER_IDS else None

    async def on_message(self, message) -> None:
        kind = type(message).__name__
        if self.cur is None:
            # Leading task/ratelimit chatter isn't the start of a renderable turn; the
            # shell set is already tracked by the controller before we get here.
            if kind in ("TaskStartedMessage", "TaskUpdatedMessage",
                        "TaskNotificationMessage", "RateLimitEvent"):
                return
            chat = self._owner_chat()
            if chat is None:
                return
            self.cur = SegmentRenderer(
                self.app.bot, chat, None,
                "🔔 Claude picked back up (a background task landed)…",
            )
            await self.cur.start()
        await self.cur.handle(message)
        if kind == "ResultMessage":
            try:
                await self.cur.finalize()
            finally:
                self.cur = None


class Watchdog:
    """Breaks Telegram silence with the Claude INSTANCE state so you never stare at a
    dead screen: every ~minute of quiet it shows `🕐 <datetime> · working|idle · shells`.
    Crucially it does NOT re-post the same status — it EDITS the last one, bumps a `×N`
    counter, and refreshes the leading datetime (a moving clock = proof of life). A
    changed status (or anything sent in between) starts a fresh message at the bottom.

    You read it and decide: idle+shells → wait (it'll wake itself when they land);
    idle+no-shells → nothing pending, poke it."""

    def __init__(self, app):
        self.app = app
        self.msg_id = None
        self.body = None          # dedupe key: status text without stamp/counter
        self.count = 1
        self.is_latest = False    # is our status message still the newest in the chat?
        self.dead_declared = False

    def _chat(self):
        return sorted(ALLOWED_USER_IDS)[0] if ALLOWED_USER_IDS else None

    def _compose(self, body, count):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        return f"🕐 {stamp} · {body}" + (f"  ×{count}" if count > 1 else "")

    def _status_body(self, st):
        if st["active"]:
            mins = int(st["segment_secs"] // 60)
            head = f"🟢 working {mins}m" if mins else "🟢 working"
        else:
            head = "💤 idle"
        shells = st["shells"]
        if shells:
            descs = ", ".join(_oneline(s.get("desc", "task"), 40) for s in shells[:4])
            more = f" (+{len(shells) - 4})" if len(shells) > 4 else ""
            tail = f"🐚 {len(shells)} shell(s): {descs}{more}"
            if not st["active"]:
                tail += " — they'll wake me when they land"
        else:
            tail = "🐚 no shells"
        return f"🐕 {head} · {tail}"

    async def loop(self):
        while True:
            await asyncio.sleep(20)
            try:
                if (time.monotonic() - _last_tg_send) < 55:
                    continue  # not silent — something already reached the phone recently
                st = controller.status()
                if not st["active"] and not st["shells"]:
                    if self.dead_declared:
                        continue
                    self.dead_declared = True
                    await self._show("💤 idle · 🐚 no shells — nothing is running, nothing "
                                     "will wake me. Say hi to continue.")
                    continue
                self.dead_declared = False
                idle_with_shells = (not st["active"]) and bool(st["shells"])
                await self._show(self._status_body(st))
                # Idle with shells for a long stretch (×N) -> nudge Claude to act.
                if idle_with_shells and self.count == IDLE_SHELLS_NUDGE_AT:
                    await self._nudge_idle_shells()
            except Exception:
                log.exception("watchdog error")

    async def _nudge_idle_shells(self):
        """~30 min idle with shells still running: ask Claude to continue, check for stuck
        shells, or clean up. Goes through the normal queue so the reply reaches the phone."""
        log.warning("watchdog: idle+shells ×%d — auto-nudging Claude", IDLE_SHELLS_NUDGE_AT)
        chat = self._chat()
        if chat is None:
            return
        try:
            await self.app.bot.send_message(
                chat, "🐕 Idle a long time with shells still running — nudging Claude to "
                "continue, check for stuck shells, or clean up.")
            mark_sent()
        except Exception:
            log.exception("nudge notice failed")
        enqueue_for_claude(chat, None, IDLE_SHELLS_NUDGE, "text", False)

    async def _show(self, body):
        chat = self._chat()
        if chat is None:
            return
        # Same status AND our message is still the newest -> edit it in place (×N + fresh
        # datetime), so we never print the same status twice.
        if self.msg_id is not None and self.is_latest and body == self.body:
            self.count += 1
            try:
                await self.app.bot.edit_message_text(
                    self._compose(body, self.count), chat_id=chat, message_id=self.msg_id)
                self._touch()
                return
            except Exception as e:
                if "not modified" not in str(e).lower():
                    log.info("watchdog edit failed: %r", e)
                # fall through to a fresh message
        # New/changed status (or no longer the newest) -> a fresh message at the bottom.
        self.count = 1
        self.body = body
        try:
            m = await self.app.bot.send_message(chat, self._compose(body, 1))
            self.msg_id = m.message_id
            self._touch()
        except Exception:
            log.exception("watchdog send failed")

    def _touch(self):
        global _last_tg_send
        _last_tg_send = time.monotonic()
        self.is_latest = True


# --- "bot ..." harness commands (intercepted before Claude) ------------------

_BOT_CMD_RE = re.compile(r"^\s*bot\b[\s,:.;!?-]*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_bot_command(text: str):
    """If the message's first word is 'bot', return the remaining command text;
    otherwise None (meaning: not a harness command, send it to Claude)."""
    m = _BOT_CMD_RE.match(text or "")
    return m.group(1).strip() if m else None


def classify_bot_command(rest: str):
    norm = rest.lower().strip(" \t.!?,;:").strip()
    if norm == "":
        return "help"
    if norm in ("new", "reset", "fresh", "clear", "new conversation", "new session",
                "start over", "start fresh"):
        return "new"
    if norm in ("stop", "cancel", "interrupt", "abort", "halt", "stop it"):
        return "stop"
    if norm in ("kill", "kill -9", "force kill", "force stop", "die", "kill claude"):
        return "kill"
    if norm in ("lock", "lockdown", "lock down", "panic", "emergency"):
        return "lock"
    if norm in ("sleep", "sleep mode", "pause", "dnd", "do not disturb", "quiet",
                "go to sleep", "sleep now"):
        return "sleep"
    if norm in ("compact", "compaction", "compact context", "compact session"):
        return "compact"
    if norm in ("restart", "restart bridge", "reboot", "reload"):
        return "restart"
    if norm in ("context", "ctx", "usage"):
        return "context"
    if norm in ("status", "state", "info", "how are you"):
        return "status"
    if norm in ("session", "sessions", "which session"):
        return "session"
    if norm in ("help", "commands", "command", "?", "menu", "what can you do"):
        return "help"
    if norm == "ping":
        return "ping"
    return None


BOT_HELP = (
    '🤖 "bot" commands — say or type, starting with the word "bot":\n'
    "• bot new / bot clear — fresh conversation (clears Claude's context)\n"
    "• bot compact — compact the conversation (summarize to free context)\n"
    "• bot stop — interrupt the current task (Esc/Ctrl-C)\n"
    "• bot kill — force-kill the Claude process (kill -9), then respawn\n"
    "• bot lock — kill Claude AND lock the bridge (unlock at the machine)\n"
    "• bot sleep — pause Telegram input (Claude keeps running); wake at the machine\n"
    "• bot effort [level] — show/set reasoning effort (low|medium|high|xhigh|max)\n"
    "• bot cwd [path] — show/set Claude's working directory\n"
    "• bot context — detailed context-window usage\n"
    "• bot logs [n] — last n bridge log lines\n"
    "• bot restart — restart the bridge process\n"
    "• bot echo <text> — echo text back (not sent to Claude)\n"
    "• start a message with \"voice\" — get a spoken reply (e.g. \"voice summarize this\")\n"
    "• bot harness <text> (or bot h) — message the AI working on this machine\n"
    "• bot status — bridge, effort, session & context\n"
    "• bot session — current session id\n"
    "• bot help — this list\n"
    'Anything not starting with "bot" is sent to Claude.'
)

# Map spoken variants to the SDK's effort levels (voice transcription is loose).
EFFORT_SYNONYMS = {
    "maximum": "max", "max": "max",
    "xhigh": "xhigh", "x high": "xhigh", "x-high": "xhigh",
    "extra high": "xhigh", "very high": "xhigh",
    "high": "high",
    "medium": "medium", "med": "medium", "normal": "medium",
    "low": "low", "minimum": "low", "min": "low", "minimal": "low",
}


async def _status_text() -> str:
    busy = "busy" if controller.busy else "idle"
    sid = controller.session_id
    sid_str = (sid[:8] + "…") if sid else "new (none yet)"
    eff = controller.get_effort() or "default"
    blocked = " · 🔒 LOCKED" if is_blocked() else ""
    sleeping = " · 😴 SLEEPING" if is_sleeping() else ""
    ctx = await controller.context_usage()
    ctx_str = f" · ctx {ctx['percentage']:.0f}%" if ctx else ""
    return (
        f"✅ Bridge OK · Claude {busy} · model {controller.model or 'default'} · "
        f"effort {eff} · session {sid_str}{ctx_str}{blocked}{sleeping} · cwd {controller.get_cwd()}"
    )


def _format_context(ctx) -> str:
    if not ctx:
        return "📊 No active session yet — context appears after your first message."
    total = ctx.get("totalTokens", 0)
    mx = ctx.get("maxTokens", 0)
    pct = ctx.get("percentage", 0)
    lines = [f"📊 Context: {total:,} / {mx:,} tokens ({pct:.0f}%)"]
    if ctx.get("isAutoCompactEnabled"):
        thr = ctx.get("autoCompactThreshold")
        lines.append(f"autocompact: on{f' (threshold {thr:,})' if thr else ''}")
    else:
        lines.append("autocompact: off")
    for c in sorted(ctx.get("categories") or [], key=lambda x: x.get("tokens", 0), reverse=True)[:6]:
        if c.get("tokens"):
            lines.append(f" • {c.get('name', '?')}: {c['tokens']:,}")
    return "\n".join(lines)


def _tail_log(n: int) -> str:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log file yet)"
    text = "\n".join(lines[-n:]).strip() or "(log empty)"
    return text[-3500:]  # keep it within one Telegram message


async def _restart_bridge() -> None:
    await asyncio.sleep(1.0)  # let the reply flush
    os._exit(0)  # the tray supervisor respawns us; the session resumes


async def maybe_handle_bot_command(context, chat_id, reply_to, text: str) -> bool:
    """Returns True if the message was a 'bot' harness command (and handled it),
    so the caller does NOT forward it to Claude."""
    rest = parse_bot_command(text)
    if rest is None:
        return False
    bot = context.bot

    async def reply(t):
        await bot.send_message(chat_id, t, reply_to_message_id=reply_to)

    # "bot echo <text>" — echo the rest back, never sent to Claude.
    m = re.match(r"^echo\b\s*(.*)$", rest, re.IGNORECASE | re.DOTALL)
    if m:
        payload = m.group(1).strip()
        await reply(payload if payload else "(nothing to echo)")
        return True

    # "bot harness <msg>" / "bot h <msg>" — message the AI/harness working on this
    # machine (the reverse of the [HARNESS] outbox). Lands in inbox/ for the AI to
    # read via cg-inbox. Never sent to Claude.
    m = re.match(r"^(?:harness|h)\b[\s:,\-]*(.*)$", rest, re.IGNORECASE | re.DOTALL)
    if m:
        payload = m.group(1).strip()
        if not payload:
            await reply("📨 Usage: bot harness <message> — sends it to the AI working on this machine.")
            return True
        try:
            HARNESS_INBOX.mkdir(parents=True, exist_ok=True)
            stamp = str(time.time_ns())
            tmp = HARNESS_INBOX / f".{stamp}.tmp"
            tmp.write_text(payload, encoding="utf-8")
            tmp.rename(HARNESS_INBOX / f"{stamp}.msg")  # atomic: AI never reads a partial
            log.info("INBOX <- user (harness): %s", _oneline(payload, 300))
            await reply("📨 Sent to the harness — the AI on this machine will see it.")
        except OSError:
            log.exception("Could not write harness inbox")
            await reply("⚠️ Couldn't queue that for the harness.")
        return True

    # "bot effort [level]" — special-cased because it takes an argument.
    m = re.match(r"^effort\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        raw = m.group(1).strip().strip(" .!?,;:")
        log.info("bot command: effort %r", raw)
        if not raw:
            cur = controller.get_effort() or "default"
            await reply(
                f"⚙️ Claude effort: {cur}\n"
                f"Levels: {', '.join(VALID_EFFORTS)}\n"
                "Set with: bot effort <level>"
            )
        else:
            level = EFFORT_SYNONYMS.get(raw.lower(), raw.lower())
            if await controller.set_effort(level):
                await reply(f"⚙️ Claude effort set to: {level} (applies going forward).")
            else:
                await reply(
                    f'⚙️ Unknown effort "{raw}". Use one of: {", ".join(VALID_EFFORTS)}.'
                )
        return True

    # "bot cwd [path]" / "bot pwd" — show or set Claude's working directory.
    m = re.match(r"^(cwd|chdir|workdir|pwd)\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        verb, target = m.group(1).lower(), m.group(2).strip().strip(" '\"")
        if verb == "pwd" or not target:
            hint = "" if verb == "pwd" else "\nSet with: bot cwd <path>"
            await reply(f"📂 Working dir: {controller.get_cwd()}{hint}")
        elif await controller.set_cwd(target):
            sid8 = (controller.session_id or "")[:8]
            await reply(
                f"📂 Working dir → {controller.get_cwd()} — conversation moved here "
                f"(🧵 {sid8 or 'none'})."
            )
        else:
            await reply(f"📂 Couldn't switch to: {target}")
        return True

    # "bot transcribe [best|good|fast]" — show or set transcription quality. Takes effect on
    # the NEXT voice message (the decoder subprocess reads its compute type fresh each spawn).
    m = re.match(r"^(?:transcribe|transcription|quality|tx)\b\s*(.*)$",
                 rest.strip(), re.IGNORECASE)
    if m:
        raw = m.group(1).strip().strip(" .!?,;:").lower()
        log.info("bot command: transcribe %r", raw)
        cur = get_compute_type()
        cur_name = _PRESET_BY_COMPUTE.get(cur, cur)
        menu = ("best — float32, most accurate (slowest)\n"
                "good — int8_float32, ~2× faster, near-best\n"
                "fast — int8, ~3-4× faster, slight accuracy loss")
        if not raw:
            await reply(
                f"🎚 Transcription quality: {cur_name} ({cur})\n{menu}\n"
                "Set with: bot transcribe <best|good|fast>"
            )
        elif raw in TRANSCRIBE_PRESETS:
            set_compute_type(TRANSCRIBE_PRESETS[raw])
            await reply(
                f"🎚 Quality → {raw} ({TRANSCRIBE_PRESETS[raw]}). "
                "Applies to your next voice message."
            )
        elif raw in _PRESET_BY_COMPUTE:  # they typed the raw compute type itself
            set_compute_type(raw)
            await reply(
                f"🎚 Quality → {_PRESET_BY_COMPUTE[raw]} ({raw}). "
                "Applies to your next voice message."
            )
        else:
            await reply(f'🎚 Unknown quality "{raw}".\n{menu}')
        return True

    # "bot logs [n]" — last N lines of the bridge log.
    m = re.match(r"^logs?\b\s*(\d*)$", rest.strip(), re.IGNORECASE)
    if m:
        n = max(1, min(int(m.group(1)), 60)) if m.group(1) else 20
        await reply("📜 last log lines:\n" + _tail_log(n))
        return True

    # "bot drop" — discard messages queued but not yet sent to Claude. Does NOT touch a turn
    # already running (that's `bot stop`); only clears the waiting batch.
    if re.match(r"^drop\b", rest.strip(), re.IGNORECASE):
        dropped = drop_pending()
        n = len(dropped)
        log.info("bot command: drop -> %d queued message(s)", n)
        if not n:
            await reply("🗑 Queue is empty — nothing to drop.")
        else:
            preview = "\n".join(f"  • {_oneline(t, 80)}" for t in dropped[:5])
            more = f"\n  …and {n - 5} more" if n > 5 else ""
            await reply(f"🗑 Dropped {n} queued message{'' if n == 1 else 's'}:\n{preview}{more}")
        return True

    action = classify_bot_command(rest)
    log.info("bot command: %r -> %s", rest, action)

    if action == "new":
        await controller.reset()
        await reply("🆕 Fresh conversation (new session).")
    elif action == "stop":
        await controller.stop()   # interrupt + clean reset (no post-interrupt wedge)
        ensure_worker()           # revive the dispatcher if the interrupt killed it
        _pending_event.set()
        await reply("✋ Stopped — turn interrupted, session kept. Send your next message.")
    elif action == "kill":
        killed = await controller.kill()
        if killed:
            await reply(
                f"💀 Killed the Claude process (kill -9, pids {killed}). "
                "It respawns — resuming the session — on your next message."
            )
        else:
            await reply("💀 No running Claude process to kill. (It respawns on your next message.)")
    elif action == "lock":
        killed = await controller.kill()
        engage_block("manual lock via 'bot lock'")
        extra = f" (killed pids {killed})" if killed else ""
        await reply(
            f"🔒 LOCKED{extra}. Claude killed and the bridge is locked. "
            "Unblock it from the tray app on the machine."
        )
    elif action == "sleep":
        engage_sleep()
        await reply(
            "😴 Sleep mode engaged — no input accepted. Claude keeps running; I'll just "
            "ignore Telegram until you press WAKE UP on the tray app at the machine."
        )
    elif action == "compact":
        # Send a raw /compact (no guard prefix) and stream the outcome.
        await dispatch_to_claude(
            context, chat_id, reply_to, "/compact", "command",
            raw=True, header="🗜 Compacting context…",
        )
    elif action == "context":
        await reply(_format_context(await controller.context_usage()))
    elif action == "restart":
        await reply("♻️ Restarting the bridge… (back in a few seconds, session resumes)")
        _spawn(_restart_bridge(), name="restart_bridge")
    elif action == "status":
        await reply(await _status_text())
    elif action == "session":
        await reply(f"🧵 session: {controller.session_id or 'new (none yet)'}")
    elif action == "help":
        await reply(BOT_HELP)
    elif action == "ping":
        await reply("🏓 pong")
    else:
        await reply(f'[claudegram] "bot" command unknown: {rest}')
    return True


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not is_authorized(update):
        uid = update.effective_user.id if update.effective_user else "?"
        log.warning("Ignoring text from unauthorized user id=%s", uid)
        await msg.reply_text("🚫 This is a private bot.")
        return
    text = (msg.text or "").strip()
    if not text:
        return
    log.info("Text from %s: %s", msg.from_user.id if msg.from_user else "?", text)
    # Sleep mode: ignore ALL Telegram input (wake only at the machine).
    if is_sleeping():
        log.info("Ignoring text — sleep mode engaged")
        await msg.reply_text(SLEEP_MSG)
        return
    # "bot ..." messages are harness commands and never reach Claude.
    if await maybe_handle_bot_command(context, msg.chat_id, msg.message_id, text):
        return
    if is_blocked():
        await msg.reply_text(BLOCKED_MSG)
        return
    voiceback, text = parse_voiceback(text)
    if voiceback and not text:
        await msg.reply_text("🔊 Say something after 'voice' for a spoken reply.")
        return
    enqueue_for_claude(msg.chat_id, msg.message_id, text, "text", voiceback)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("🚫 This is a private bot.")
        return
    await controller.reset()
    await update.message.reply_text("🆕 Fresh conversation (new session).")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("🚫 This is a private bot.")
        return
    await controller.stop()
    ensure_worker()
    _pending_event.set()
    await update.message.reply_text("✋ Stopped — turn interrupted, session kept.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("🚫 This is a private bot.")
        return
    await update.message.reply_text(await _status_text())


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)


# --- lifecycle: startup ping + shutdown diagnostics --------------------------

_START_TS = time.monotonic()
_exit_logged = False


def _log_exit(signum: int | None = None) -> None:
    """Log that claudegram is exiting, with whatever diagnostics we have. Fires on
    normal exit (atexit) and on catchable signals. (SIGKILL can't be caught — the
    tray supervisor logs that case instead.)"""
    global _exit_logged
    if _exit_logged:
        return
    _exit_logged = True
    up = time.monotonic() - _START_TS
    sig = ""
    if signum is not None:
        try:
            sig = f" · signal={signal.Signals(signum).name}({signum})"
        except ValueError:
            sig = f" · signal={signum}"
    log.warning(
        "🛑 claudegram exited — uptime %.0fs · session=%s · cwd=%s%s",
        up, controller.session_id or "none", controller.get_cwd(), sig,
    )


async def deliver_harness(application, body: str) -> None:
    """Send a [HARNESS] message to the owner(s) on Telegram."""
    text = "🤖 [HARNESS] " + body.strip()
    for uid in sorted(ALLOWED_USER_IDS):
        try:
            await reply_chunked_bot(application.bot, uid, None, text)
        except Exception:
            log.exception("Failed to deliver HARNESS message to %s", uid)
    mark_sent()
    log.info("HARNESS -> %s: %s", sorted(ALLOWED_USER_IDS), _oneline(body, 200))


async def harness_outbox_loop(application) -> None:
    """IPC inbox for OTHER programs on this machine. Anything (an AI like Claude Code
    working here, a cron job, a build script) can ping the phone by dropping a message
    file in the outbox dir; we relay it as a [HARNESS] message, then delete the file.

    Convention (atomic, so we never read a half-written file): write to a name that
    starts with '.' or ends in '.tmp', then rename to the final name. The helper
    `cg-notify` does this for you:  ./cg-notify "build finished, all green"
    Or by hand:  echo 'hi' > outbox/.x && mv outbox/.x outbox/x.msg
    """
    try:
        HARNESS_OUTBOX.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("Could not create outbox dir %s", HARNESS_OUTBOX)
        return
    while True:
        try:
            for f in sorted(HARNESS_OUTBOX.iterdir()):
                if (not f.is_file()) or f.name.startswith(".") or f.name.endswith(".tmp"):
                    continue  # not a finished drop
                try:
                    body = f.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                try:
                    f.unlink()  # consumed (delete before sending: never double-send)
                except OSError:
                    pass
                if body:
                    await deliver_harness(application, body)
        except Exception:
            log.exception("harness outbox loop error")
        await asyncio.sleep(1.0)


async def on_startup(application) -> None:
    """Runs once the bot is initialized — log it and ping the owner(s) on Telegram
    that the bridge just came online (handy to see power-cycles from your phone)."""
    sid = controller.session_id
    log.info("🟢 claudegram online — session=%s cwd=%s", sid or "new", controller.get_cwd())
    text = (
        "🟢 claudegram online\n"
        f"🧵 session: {sid or 'new (none yet)'}\n"
        f"📂 cwd: {controller.get_cwd()}\n"
        f"🤖 model: {controller.model or 'Opus 4.8 (Claude Code default)'}\n"
        f"⚙️ effort: {controller.get_effort() or 'default'}\n"
        f"🎙 transcribe: {MODEL_SIZE}/{get_compute_type()} "
        f"({_PRESET_BY_COMPUTE.get(get_compute_type(), '?')})"
        + ("\n🔒 LOCKED — unblock at the machine to resume" if is_blocked() else "")
        + ("\n😴 SLEEPING — input paused; press WAKE UP on the tray to resume" if is_sleeping() else "")
    )
    for uid in sorted(ALLOWED_USER_IDS):
        try:
            await application.bot.send_message(uid, text)
        except Exception:
            log.exception("Could not send startup ping to %s", uid)
    mark_sent()  # the online ping counts — don't let the watchdog fire immediately
    global _app
    _app = application
    # Single dispatcher: batches a burst of messages into one Claude turn.
    ensure_worker()
    _spawn(worker_guard(), name="worker_guard")
    log.info("dispatch worker + guard started (debounce %.1fs)", BATCH_DEBOUNCE)
    # Relay the turns Claude starts on its OWN (a background shell landed) to the phone.
    controller.set_spontaneous_handler(SpontaneousRelay(application).on_message)
    # Start the IPC inbox so other programs on this machine can ping the phone.
    _spawn(harness_outbox_loop(application), name="harness_outbox")
    log.info("HARNESS outbox watcher started at %s", HARNESS_OUTBOX)
    # Start the watchdog: break Telegram silence with the Claude instance's state
    # (edits one status in place with ×N + a moving datetime instead of re-posting).
    global _watchdog
    _watchdog = Watchdog(application)
    _spawn(_watchdog.loop(), name="watchdog")
    log.info("watchdog started (silence-breaker: datetime + working/idle + shells, ×N dedupe)")


def main() -> None:
    token = load_token()

    # This build drives a Claude Code instance that can RUN COMMANDS, so an
    # allowlist is mandatory — we refuse to start open (no fail-open for code exec).
    if not ALLOWED_USER_IDS:
        raise SystemExit(
            "Refusing to start: ALLOWED_USER_IDS is empty, but this build drives a "
            "Claude Code instance that can run commands. Set ALLOWED_USER_IDS in .env "
            "to your Telegram user id to lock the bridge to your account."
        )

    removed = force_subscription_env()
    if removed:
        log.warning("Stripped API-routing env vars (forcing subscription use): %s", removed)
    ensure_cghome()
    sweep_audio_tmp()
    log.info("Private mode: only user ids %s are served.", sorted(ALLOWED_USER_IDS))
    log.info("Claude working dir: %s", CGHOME)
    if is_blocked():
        log.warning("Starting in BLOCKED state — Unblock from the tray app to resume.")

    # Transport tuning. The API-call request (send/edit) already pools 256 conns by
    # default, so it isn't the bottleneck — but PTB's default read/write timeouts are
    # only 5s, so a slow or bursty send (status-board edits + streamed paragraphs all
    # firing during a turn) can hit that ceiling and drop a message. Make the pool
    # explicit and give sends generous timeouts so streamed output flows reliably.
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(on_startup)            # ping the owner(s) that the bridge is online
        .concurrent_updates(True)         # so /stop runs while a turn is in progress
        .connection_pool_size(256)        # explicit (PTB default; documents intent)
        .pool_timeout(30.0)               # wait for a slot under burst, don't error early
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .media_write_timeout(120.0)       # voice downloads can be chunky
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.VIDEO,
            handle_audio,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    # Shutdown diagnostics: log on normal exit, and on catchable signals (we handle
    # them ourselves so we can record which signal — stop_signals=None below).
    atexit.register(_log_exit)

    def _on_signal(signum, frame):
        _log_exit(signum)          # flushes the exit line to the log first
        os._exit(128 + signum)     # then exit hard (closes the poll socket cleanly)

    for _s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_s, _on_signal)
        except (ValueError, OSError):
            pass

    # Diagnostic: `kill -USR1 <bot-pid>` dumps every asyncio task to the log — so a future
    # "the worker vanished" mystery can be confirmed live (is dispatch_worker present?).
    def _dump_tasks(signum, frame):
        try:
            tasks = asyncio.all_tasks()
            names = sorted((t.get_name() + ("" if not t.done() else "(done)")) for t in tasks)
            log.warning("SIGUSR1 task dump (%d): %s", len(tasks), ", ".join(names))
        except Exception:
            log.exception("task dump failed")

    try:
        signal.signal(signal.SIGUSR1, _dump_tasks)
    except (ValueError, OSError):
        pass

    log.info("claudegram bridge is up. Talk to your bot (voice or text).")
    # drop_pending_updates=False: messages sent while we were offline are held by
    # Telegram (for ~24h) and delivered when we reconnect, so nothing is skipped.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, stop_signals=None
    )


if __name__ == "__main__":
    main()
