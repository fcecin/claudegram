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
import collections
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
    ambient_default_model,
    default_model_guard,
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
# Friendly transcription-quality presets, toggled live per-bot via `bot transcribe <name>`.
# The change takes effect on the NEXT voice message — the worker reads its compute type fresh
# on every spawn, so nothing needs restarting.
TRANSCRIBE_PRESETS = {       # name -> ctranslate2 compute type
    "best": "float32",       # large-v3 full precision — slowest, most accurate
    "good": "int8_float32",  # ~2x faster, near-best accuracy
    "fast": "int8",          # ~3-4x faster, a little accuracy lost
}
_PRESET_BY_COMPUTE = {v: k for k, v in TRANSCRIBE_PRESETS.items()}

# Immutable code default for transcription (best/float32 — fully ours, always available).
# A bot without a `transcribe` config inherits this at spawn; `bot transcribe` overrides a
# bot's live value for the process only (never touches this default, never persists).
DEFAULT_COMPUTE = TRANSCRIBE_PRESETS["best"]


def get_compute_type() -> str:
    """The immutable global default compute type (best). Bots override live via bot transcribe."""
    return DEFAULT_COMPUTE


def session_compute(session) -> str:
    """A bot's live compute: its own runtime/config value if set, else the code default."""
    return getattr(session, "compute", None) or DEFAULT_COMPUTE


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

# Default working dir for the driven Claude: an install-local, gitignored `work/` (so each
# copy is self-contained and nothing leaks into git). Override with the CGHOME env var.
CGHOME = Path(os.environ.get("CGHOME", str(HERE / "work"))).expanduser()
SESSION_FILE = HERE / "session.id"   # persisted Claude session id (for resume)
EFFORT_FILE = HERE / "effort.level"  # persisted reasoning effort
CWD_FILE = HERE / "cwd.path"         # persisted working directory
LOG_PATH = HERE / "claudegram.log"   # bridge log (written by the tray supervisor)
AUDIO_TMP = Path(tempfile.gettempdir()) / "claudegram_audio"  # transient voice files
VOICE_TMP = Path(tempfile.gettempdir()) / "claudegram_voiceback"  # transient TTS output
IMAGE_TMP = Path(tempfile.gettempdir()) / "claudegram_images"  # incoming images (kept until Claude's turn Reads them; swept at startup)
IMAGE_MAX_AGE = 6 * 3600  # also prune cached images older than this on each new one (self-bound for long no-restart sessions)
HARNESS_OUTBOX = HERE / "outbox"     # drop dir: any program leaves a msg -> sent to phone
HARNESS_INBOX = HERE / "inbox"       # drop dir: "bot harness <msg>" -> read by the AI here
MEDIA_OUTBOX = HERE / "media-outbox"
CMD_INBOX = HERE / "cmd-inbox"       # drop dir: the DRIVEN Claude drops a config command
                                     # (via ./cg-cmd) -> run through the bot-command handler
WAKE_INBOX = HERE / "wake-inbox"     # drop dir: a scheduler (cron via ./cg-wake) or a peer
                                     # program drops a msg -> injected as a turn into the
                                     # current bot session (the external -> bot-turn path)
# --- multi-session multiplexing (ONE Telegram bot, N concurrent Claude sessions) -----
# Names + color bubbles. "claude" (orange) is the DEFAULT/hidden session: until a SECOND
# session is created, registry.multiplexing() is False and every badge is "" — so the whole
# bridge renders EXACTLY like a single-Claude install. The moment a sibling exists, every
# bot-authored artifact is prefixed with "<emoji> <name> · " so an interleaved scroll is
# legible. Sessions run CONCURRENTLY: each is its own ClaudeController (own session id / cwd /
# effort) with its own dispatch queue + worker + watchdog + spontaneous relay.
# The roster is scanned from bots/*/ (discover_bots); each bot's directory is its definition —
# icon, aliases, model, effort, and the internal flag all live in its config.json.
DEFAULT_SESSION = "claude"    # the default/hidden session; its config lives in bots/claude/
DEFAULT_ICON = "⚙️"           # badge for a bot whose config declares no icon
# Seed used to regenerate the default bot's config if bots/claude/config.json goes missing (fresh
# checkout, accidental deletion). It's only the seed — once the file exists, the file is truth.
_DEFAULT_CLAUDE_CONFIG = {
    "icon": "🟠",
    "aliases": ["claud", "clode", "cloude", "claudee", "clod",
                "cloud", "clawd", "clawed", "klaus", "klaude"],
}

# --- anti-stall guard -----------------------------------------------------------------------
NOSTALL_BOT = "jack"     # the guard bot, by name; its config lives in bots/jack/ (no dir → can't enable)
NOSTALL_FILE = HERE / "nostall.mode"     # presence = guard ON (global sticky, like voice)
NOSTALL_FEED_MSGS = 6                     # how many of a bot's latest answers the guard reviews
NOSTALL_COOLDOWN_SECS = 180              # min seconds between interventions on ONE bot
NOSTALL_LEGIT_MARKER = "LEGIT STOP"      # verdict meaning "release it — genuinely done"


def resolve_session_name(raw: str):
    """Map a raw (possibly mis-transcribed) token to a currently-available selectable bot, or
    None. Everything comes from the boot scan: exact name, then a config-declared alias, then a
    fuzzy match — all against the bots that exist right now."""
    tok = (raw or "").strip().split()
    if not tok:
        return None
    n = tok[0].strip(" .!?,;:'\"").lower()
    sel = selectable_bots()
    if n in sel:                  # internal/system bots aren't in `sel`, so they never resolve
        return n
    aliases = session_aliases()
    if n in aliases and aliases[n] in sel:
        return aliases[n]
    import difflib
    close = difflib.get_close_matches(n, sel, n=1, cutoff=0.8)
    return close[0] if close else None


def _session_files(name: str):
    """(session_file, effort_file, cwd_file) for a session. The DEFAULT reuses the original
    single-session files, so an existing install's live conversation simply becomes 'claude';
    named sessions get namespaced siblings (session.<name>.id, …)."""
    if name == DEFAULT_SESSION:
        return str(SESSION_FILE), str(EFFORT_FILE), str(CWD_FILE)
    return (str(HERE / f"session.{name}.id"),
            str(HERE / f"effort.{name}.level"),
            str(HERE / f"cwd.{name}.path"))


BOTS_DIR = HERE / "bots"    # every bot is a subdirectory here (config.json + optional main.md)


def bot_config(name: str) -> dict:
    try:
        return json.loads((BOTS_DIR / name / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def ensure_default_bot() -> None:
    """Self-heal the default 'claude' bot before the roster is read: guarantee its dir,
    config.json, and var/.gitkeep exist, regenerating only what's missing. Scoped to the default
    bot — other bots are simply whatever is on disk."""
    d = BOTS_DIR / DEFAULT_SESSION
    keep = d / "var" / ".gitkeep"
    if not keep.is_file():
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.touch()
    cfg = d / "config.json"
    if not cfg.is_file():
        cfg.write_text(json.dumps(_DEFAULT_CLAUDE_CONFIG, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        log.info("bootstrap: regenerated %s", cfg)


# Shorthands resolved to an exact model ID before reaching the CLI (which may not
# know the bare alias). Defined here because Session() runs at import time.
# fable is safe to expose (incl. self-config): it draws usage credits, and with
# credits disabled it just stops — it can no longer bill overage (old 4daac98
# exclusion is obsolete).
MODEL_ALIASES = {"fable": "claude-fable-5"}


class Session:
    """One named Claude instance behind the single Telegram bot."""

    def __init__(self, name: str):
        self.name = name
        self.config = bot_config(name)
        self.emoji = self.config.get("icon") or DEFAULT_ICON
        self.internal = bool(self.config.get("internal"))
        self.empty_reply = self.config.get("empty_reply")
        # Ring of this bot's most-recent answers — what the guard reviews when it goes idle.
        self.recent_answers = collections.deque(maxlen=NOSTALL_FEED_MSGS)
        # Live transcription compute: config value at spawn, else None => the code default.
        # `bot transcribe` overrides this in-memory (process-lived); never persisted.
        self.compute = TRANSCRIBE_PRESETS.get((self.config.get("transcribe") or "").lower())
        sf, ef, cf = _session_files(name)
        model = self.config.get("model")
        self.controller = ClaudeController(str(CGHOME), sf, ef, cf,
                                           model=MODEL_ALIASES.get(model, model),
                                           max_budget_usd=self.config.get("max_budget_usd"),
                                           effort=self.config.get("effort"))
        # per-session batching queue (mirrors the old module-level _pending* globals)
        self.pending: list[dict] = []
        self.pending_event = asyncio.Event()
        self.pending_since = 0.0
        self.worker_task = None      # session_worker() draining this queue
        self.watchdog = None         # Watchdog for this session
        self.watchdog_task = None
        self.relay = None            # SpontaneousRelay for this session
        self.no_more_work = False    # this session's Claude declared it's out of work
        self.parked = False          # user forced end-state idle: no nudging, no anti-stall (bot park)
        self.nostall_cleared = False # guard reviewed this idle episode and ruled it genuinely done

    def __repr__(self):
        return f"<Session {self.emoji}{self.name}>"


class SessionRegistry:
    """The set of live sessions + which one is CURRENT (receives undecorated input)."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.current_name = DEFAULT_SESSION

    def ensure_default(self) -> "Session":
        if DEFAULT_SESSION not in self.sessions:
            self.sessions[DEFAULT_SESSION] = Session(DEFAULT_SESSION)
        return self.sessions[DEFAULT_SESSION]

    def current(self) -> "Session":
        return self.sessions[self.current_name]

    def get(self, name: str) -> "Session | None":
        return self.sessions.get(name)

    def multiplexing(self) -> bool:
        # Internal bots don't count — a solo install stays single-session (no badges) even
        # while the anti-stall guard is running one in the background.
        return len([s for s in self.sessions.values() if not s.internal]) > 1

    def known(self, name: str) -> bool:
        return name in discover_bots()

    def badge(self, session: "Session") -> str:
        """Color-bubble tag — '' unless multiplexing, so the default path is untouched."""
        return f"{session.emoji} {session.name} · " if self.multiplexing() else ""


ensure_default_bot()   # regenerate bots/claude/ if missing BEFORE the default session reads it
registry = SessionRegistry()
registry.ensure_default()
# `controller` ALWAYS tracks the CURRENT session (reassigned by select_session), so the many
# command / status / lifecycle references keep operating on "the session you're talking to".
controller = registry.current().controller


def select_session(name: str) -> "Session":
    """Switch the CURRENT session, creating it (from the palette) on first use and wiring up
    its worker + watchdog + relay. Returns the Session. Reassigns the module `controller`."""
    global controller
    created = name not in registry.sessions
    if created:
        registry.sessions[name] = Session(name)
    registry.current_name = name
    controller = registry.sessions[name].controller
    if created:
        _activate_session(registry.sessions[name])
    return registry.sessions[name]


def _activate_session(session: "Session") -> None:
    """Start a session's queue worker, watchdog, and spontaneous relay. Called once per
    session (at startup for the default, on creation for the rest)."""
    ensure_worker(session)
    if _app is not None:
        session.relay = SpontaneousRelay(_app, session)
        session.controller.set_spontaneous_handler(session.relay.on_message)
        session.watchdog = Watchdog(_app, session)
        session.watchdog_task = _spawn(session.watchdog.loop(), name=f"watchdog[{session.name}]")


async def end_session(name: str) -> str:
    """Tear down a non-default session: kill its Claude, cancel its worker/watchdog, drop it.
    Returns a human status string. If it was current, fall back to the default."""
    if name == DEFAULT_SESSION:
        return "can't end the default 'claude' session"
    session = registry.sessions.get(name)
    if session is None:
        return f"no live session named {name}"
    try:
        await session.controller.kill()
    except Exception:
        log.exception("end_session: kill failed for %s", name)
    for t in (session.worker_task, session.watchdog_task):
        if t is not None and not t.done():
            t.cancel()
    if session.watchdog is not None and session.watchdog.msg_id is not None:
        try:
            await _app.bot.delete_message(session.watchdog._chat(), session.watchdog.msg_id)
        except Exception:
            pass
    registry.sessions.pop(name, None)
    if registry.current_name == name:
        select_session(DEFAULT_SESSION)
    return f"ended {session.emoji}{name}"

# Silence tracker for the watchdog: monotonic ts of the last NEW message sent to the
# owner. Edits don't count (they don't notify). The 60s watchdog only speaks after a gap.
_last_tg_send = time.monotonic()


def mark_sent() -> None:
    """Record that a (non-watchdog) message reached the owner. This also tells EVERY
    session's watchdog its last status message is no longer the newest, so its next status
    starts a fresh message instead of editing one now buried above other content."""
    global _last_tg_send
    _last_tg_send = time.monotonic()
    for s in registry.sessions.values():
        if s.watchdog is not None:
            s.watchdog.is_latest = False


# Autonomy nudge state (ephemeral, NOT persisted, PER SESSION): set when THAT session's Claude
# declares it's out of work (its reply leads with NO_MORE_WORK_MARKER), cleared the instant the
# user sends new work to it. It ONLY controls whether that session's idle watchdog auto-nudges —
# it NEVER gates input. Per-session so one Claude saying "done" doesn't silence another's nudger.
def set_no_more_work(session, v: bool) -> None:
    if session is not None:
        session.no_more_work = v


def is_no_more_work(session) -> bool:
    return bool(session is not None and session.no_more_work)


# Recent tool errors ("issues"), shown on demand with `bot issues` (which DRAINS them, like
# an inbox) instead of bloating every turn summary. In-memory, bounded, ephemeral — the turn
# summary shows only the count; the detail lives here.
_recent_issues: list = []   # (HH:MM:SS, "tool: snippet")
ISSUES_KEEP = 100


def record_issue(text: str) -> None:
    _recent_issues.append((time.strftime("%H:%M:%S"), text))
    if len(_recent_issues) > ISSUES_KEEP:
        del _recent_issues[:-ISSUES_KEEP]


# Audio transcription in-flight counter. While >0 the bot is busy decoding voice — which is
# NOT a Claude turn, so controller.status() reads "idle". The idle watchdog checks this to
# FREEZE its ×N counters and skip nudging while a transcription runs (the transcription
# bubble already shows liveness). Plain int on the single event loop; inc/dec never await.
_transcribing = 0


def transcribe_active() -> bool:
    return _transcribing > 0


def transcribe_begin() -> None:
    global _transcribing
    _transcribing += 1


def transcribe_end() -> None:
    global _transcribing
    _transcribing = max(0, _transcribing - 1)


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
# After ~×30 idle ticks (~30 min) with NOTHING running, nudge Claude to continue or to
# declare it's done. NO_MORE_WORK_MARKER is the agreed opt-out: detected ANYWHERE in the reply
# (substring scan in SegmentRenderer.finalize) since a bot often buries it mid-paragraph.
NO_MORE_WORK_MARKER = "NO MORE WORK"
IDLE_NO_SHELLS_NUDGE_AT = 30
IDLE_AUTOEND_AT = 10  # a background (non-current, non-default) session idle+no-shells this many
#                       watchdog ticks is auto-ended to free resources (re-select recreates it)
IDLE_NO_SHELLS_NUDGE = (
    "You have been idle for a long time with nothing running (no background shells). "
    "If you have any remaining work or next steps, CONTINUE now. If you are genuinely out "
    "of work and ideas, include the exact words 'NO MORE WORK' (uppercase) anywhere in your "
    "reply and I will stop nudging you until the human sends something."
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
# Each Session owns its OWN pending queue / event / worker (see class Session) so sessions
# run concurrently. What stays GLOBAL is genuinely shared: ONE whisper decode at a time (CPU),
# and the Telegram Application handle.
_transcribe_lock = asyncio.Lock()  # serialize audio decoding: ONE at a time, in message order
_app = None  # the telegram Application; set in on_startup so workers can send
# asyncio keeps only WEAK refs to tasks — an unreferenced long-lived task can be garbage
# collected mid-flight ("Task was destroyed but it is pending!"). Keep strong refs here.
_bg_tasks: set = set()


def _spawn(coro, name=None):
    """Create a background task and KEEP A STRONG REFERENCE so the GC can't eat it."""
    t = asyncio.create_task(coro, name=name)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


# --- subscription usage (5h session / weekly), scraped from `claude /usage` ------
# The Agent SDK doesn't expose subscription utilisation (the CLI sends
# utilization=None while status=allowed, and the anthropic-ratelimit-unified-*
# headers live inside the CLI subprocess, out of our reach). But `/usage` renders
# the numbers as plain text, so usage_worker.py boots a THROWAWAY `claude` TUI in
# tmux and scrapes them (no prompt sent => no tokens). A background task refreshes
# the cache every USAGE_REFRESH_SECS; the DONE summary just reads the cache, so a
# slow ~8s scrape never blocks a turn. The print site is intentionally decoupled
# (format_usage) so it can move later.
USAGE_REFRESH_SECS = 600     # 10 min — the 5h/week windows move slowly
USAGE_SCRAPE_TIMEOUT = 90    # hard cap on one scrape (TUI boot + panel render)
_usage_cache: dict = {}      # last good scrape: {session_pct, session_reset, week_pct, week_reset, ts}


async def _scrape_usage_once() -> None:
    """Run usage_worker.py as a subprocess and cache the parsed result. Never raises."""
    global _usage_cache
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "usage_worker.py"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), USAGE_SCRAPE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            log.warning("usage scrape timed out after %ss — keeping last cache", USAGE_SCRAPE_TIMEOUT)
            return
        lines = (out or b"").decode("utf-8", "replace").strip().splitlines()
        data = json.loads(lines[-1]) if lines else {}
        if data.get("session_pct") is not None and data.get("week_pct") is not None:
            _usage_cache = data
            log.info("usage refreshed: session=%s%% (%s) week=%s%% (%s)",
                     data.get("session_pct"), data.get("session_reset"),
                     data.get("week_pct"), data.get("week_reset"))
        else:
            log.warning("usage scrape returned no numbers: %s", data)
    except Exception:
        log.exception("usage scrape failed — keeping last cache")


async def usage_collector_loop() -> None:
    """Refresh the subscription-usage cache every USAGE_REFRESH_SECS, forever."""
    while True:
        await _scrape_usage_once()
        await asyncio.sleep(USAGE_REFRESH_SECS)


def _reset_paren(hours, clock) -> str:
    """`(⟳3.9h)` / `(⟳6.9d)` from hours-until; fall back to the raw clock string."""
    if hours is None:
        return f" (⟳{clock})" if clock else ""
    if hours >= 48:
        return f" (⟳{hours / 24:.1f}d)"
    return f" (⟳{hours:.0f}h)" if hours >= 10 else f" (⟳{hours:.1f}h)"


def format_usage() -> str:
    """Compact ` · 5h 15% (⟳3.9h) · wk 4% (⟳6.9d)` for the DONE line; '' if unknown."""
    u = _usage_cache
    if not u:
        return ""
    parts = []
    if u.get("session_pct") is not None:
        parts.append(f"5h {u['session_pct']}%" + _reset_paren(u.get("session_hours"), u.get("session_reset")))
    if u.get("week_pct") is not None:
        parts.append(f"wk {u['week_pct']}%" + _reset_paren(u.get("week_hours"), u.get("week_reset")))
    return (" · " + " · ".join(parts)) if parts else ""


def enqueue_for_claude(session, chat_id, reply_to, text: str, source: str, voiceback: bool) -> None:
    """Queue a message onto a SPECIFIC session's batch (its worker drains it). Routing =
    just picking the session; the current session is the usual target."""
    if not session.pending:
        session.pending_since = time.monotonic()
    session.pending.append({
        "chat_id": chat_id, "reply_to": reply_to, "text": text,
        "source": source, "voiceback": voiceback,
    })
    session.pending_event.set()


def drop_pending(session) -> list[str]:
    """Discard messages queued but NOT yet dispatched to a session's Claude; return their
    texts. Safe from a handler: same event loop as the worker, and the clear is a single
    non-awaiting statement (no race with the drain)."""
    texts = [m["text"] for m in session.pending]
    session.pending[:] = []
    session.pending_since = 0.0
    return texts


async def session_worker(session) -> None:
    """One dispatcher PER session: waits for queued messages, lets a burst settle, then sends
    the WHOLE queue to that session's Claude as one combined turn. Serializes that session's
    user turns (one at a time); different sessions run concurrently.

    The ENTIRE loop body is guarded: an exception in any iteration is logged and the worker
    keeps going. It must never die silently — a dead worker = messages received but never
    dispatched (that session's queue stalls forever)."""
    ctx = types.SimpleNamespace(bot=_app.bot)
    while True:
        try:
            await session.pending_event.wait()
            session.pending_event.clear()
            await asyncio.sleep(BATCH_DEBOUNCE)  # gather the burst
            if not session.pending:
                continue
            batch, session.pending[:] = session.pending[:], []
            session.pending_since = 0.0
            parts = [m["text"].strip() for m in batch if m["text"].strip()]
            if not parts:
                continue
            combined = "\n\n".join(parts)
            voiceback = voice_mode_on() or any(m["voiceback"] for m in batch)
            source = "audio" if any(m["source"] == "audio" for m in batch) else "text"
            chat_id, reply_to = batch[-1]["chat_id"], batch[-1]["reply_to"]
            header = "🤖 Claude is working…"
            if len(batch) > 1:
                header += f" · 📨 {len(batch)} msgs"
            log.info("worker[%s]: dispatching %d message(s) to Claude", session.name, len(batch))
            await dispatch_to_claude(ctx, session, chat_id, reply_to, combined, source,
                                     header=header, voiceback=voiceback)
        except asyncio.CancelledError:
            log.warning("session_worker[%s] got CancelledError — exiting (guard/ensure_worker revives)",
                        session.name)
            raise  # genuine shutdown — let it propagate
        except Exception:
            log.exception("session_worker[%s] iteration failed — continuing (worker stays alive)",
                          session.name)
            await asyncio.sleep(1)  # avoid a tight error loop


def ensure_worker(session) -> None:
    """(Re)create a session's dispatch worker if it's not running. Idempotent and cheap.
    Called at activation, by `bot stop`, and by the guard — so a dead/cancelled worker is
    revived immediately rather than waiting for the guard's next tick."""
    if session.worker_task is None or session.worker_task.done():
        session.worker_task = asyncio.create_task(
            session_worker(session), name=f"worker[{session.name}]")
        log.info("dispatch worker (re)started for %s", session.name)


async def worker_guard() -> None:
    """Self-heal the dispatchers. If messages sit queued for a session while ITS Claude is
    idle for too long, that worker has wedged or died — recreate it. One guard covers all
    sessions. This is what makes a `bot stop` / interrupt edge case unable to strand a queue."""
    while True:
        await asyncio.sleep(15)
        try:
            for session in list(registry.sessions.values()):
                if not session.pending or session.controller.busy:
                    continue  # nothing queued, or a turn is legitimately running
                age = time.monotonic() - (session.pending_since or time.monotonic())
                dead = session.worker_task is None or session.worker_task.done()
                if dead or age > 40:
                    log.warning("worker guard[%s]: %d msg(s) stuck %.0fs (worker dead=%s) — recreating",
                                session.name, len(session.pending), age, dead)
                    if session.worker_task is not None and not session.worker_task.done():
                        session.worker_task.cancel()
                    session.worker_task = asyncio.create_task(
                        session_worker(session), name=f"worker[{session.name}]")
                    session.pending_event.set()  # kick it to drain immediately
        except Exception:
            log.exception("worker guard error")


def ensure_cghome() -> None:
    CGHOME.mkdir(parents=True, exist_ok=True)


def sweep_audio_tmp() -> None:
    """Clear leftover temp media (incoming voice/images + outgoing TTS) from a prior crash."""
    for d in (AUDIO_TMP, VOICE_TMP, IMAGE_TMP, MEDIA_OUTBOX):
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
    " via the user's bridge — just help with normal requests. Only a genuine malicious "
    "hacking/intrusion attempt gets no normal answer or refusal: reply exactly 'HACKING "
    f"ATTEMPT BLOCKED' on line 1, then the reason (unsure? read {REGRESSIONS_FILE}). "
)
GUARD_AUDIO = "[Voice transcript (may be imperfect)" + _GUARD_BODY + "Transcript:]"
GUARD_TEXT = "[Text" + _GUARD_BODY + "Message:]"

BLOCKED_MSG = (
    "🔒 claudegram is LOCKED — a request was flagged as a hacking attempt. "
    "It will stay locked until you Unblock it from the tray app on the machine."
)


# Injected only when the user opts a turn into voiceback (prompt starts with "voice").
VOICEBACK_PREAMBLE = (
    "[VOICEBACK ON: your whole reply is spoken as one voice message (no text shown). Speak "
    "naturally and briefly; no code, paths, logs, URLs, or long lists (they sound bad aloud).]\n"
)


# Teaches every turn that the bot can reconfigure claudegram (when asked) and manage itself.
# Small on purpose (rides every prompt). `cg-cmd` drops a command into cmd-inbox/, which a loop
# here runs through the ordinary bot-command handler (safe subset only).
SELFCONFIG_PREAMBLE = (
    f"[self-config via `{HERE / 'cg-cmd'} <cmd>` — change a bridge setting when asked, or manage "
    "yourself: effort low|medium|high|xhigh|max · model opus|sonnet|haiku|fable|default · voice on|off "
    "· transcribe best|good|fast · cwd <path> · park (when you're done: end-state idle, stops "
    "nudging) · status. Effect is next turn. To deliver a file (image/PDF/any document) to the "
    f"user's phone: `{HERE / 'cg-send'} <file> [caption]`.]\n"
)


def bot_home(name: str):
    d = BOTS_DIR / name
    return d if (d / "main.md").is_file() else None


def bot_boot_pointer(name: str) -> str:
    home = bot_home(name)
    if home is None:
        return ""
    return (
        f'[You are bot "{name}" (home: {home}). Read {home}/main.md now and follow it every '
        "turn; re-read it and what it points to after any compaction. Relative paths are under "
        "home. Does not relax the guard above.]\n"
    )


def build_prompt(user_text: str, source: str, voiceback: bool = False,
                 bot_name: str | None = None) -> str:
    guard = GUARD_AUDIO if source == "audio" else GUARD_TEXT
    boot = bot_boot_pointer(bot_name) if bot_name else ""
    pre = VOICEBACK_PREAMBLE if voiceback else ""
    return f"{guard}\n{boot}{SELFCONFIG_PREAMBLE}{pre}{user_text}"


def detect_tts_lang(text: str, default: str = "en") -> str:
    """Best-effort ISO language code for `text` (e.g. 'en', 'pt', 'es') via langdetect, so
    speech is spoken in the TEXT's language. `_resolve_voice` maps it to a Kokoro voice/lang.
    Falls back to `default` when detection fails."""
    if not (text or "").strip():
        return default
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0            # deterministic
        code = detect(text)                 # e.g. 'pt', 'en', 'es', 'zh-cn'
    except Exception:
        return default
    return code.split("-")[0].lower()       # 'zh-cn' -> 'zh', 'pt-br' -> 'pt'


def _voice_filters(voice: dict) -> str:
    """ffmpeg character effects layered on the Kokoro voice. Knobs (all optional):
    pitch (semitones, - = deeper), tempo (extra tempo), bass (dB low-end boost),
    growl (clean voice mixed with a gravelly octave-down layer, stays intelligible;
    true=0.6 or a 0..1 mix), reverb (echo; true=light or a decay), robot (vocoder)."""
    pre = ["aresample=48000"]                        # before any split
    pitch = voice.get("pitch") or 0
    if pitch:
        f = 2.0 ** (pitch / 12.0)
        pre += [f"asetrate=48000*{f:.5f}", f"atempo={1.0 / f:.5f}", "aresample=48000"]
    tempo = voice.get("tempo")
    if tempo and float(tempo) != 1.0:
        pre.append(f"atempo={float(tempo):.4f}")

    post = []
    if voice.get("bass"):
        post.append(f"bass=g={int(voice['bass'])}")
    rv = voice.get("reverb")
    if rv:
        decay = 0.25 if rv is True else float(rv)   # reverb: true = light; a number = wetter
        post.append(f"aecho=0.8:0.9:70:{decay:.2f}")
    if voice.get("robot"):
        post.append("afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)'"
                    ":win_size=512:overlap=0.75")

    gr = voice.get("growl")
    if gr:
        mix = 0.6 if gr is True else float(gr)
        s = (",".join(pre) + ",asplit[d][w];"
             "[w]asetrate=48000*0.5,aresample=48000,atempo=2.0,acrusher=bits=7:mode=log:mix=0.5[s];"
             f"[d][s]amix=inputs=2:weights=1 {mix:.2f}:normalize=0")
        if post:
            s += "," + ",".join(post)
    else:
        s = ",".join(pre + post)
    return s if s == "aresample=48000" else s + ",alimiter=limit=0.97"  # clip guard (bass/pitch)


KOKORO_DIR = Path(os.environ.get("KOKORO_MODEL_DIR") or (HERE / "models"))
KOKORO_ONNX = KOKORO_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = KOKORO_DIR / "voices-v1.0.bin"
DEFAULT_VOICE = "af_heart"
_KOKORO_LANG = {"en": "en-us", "pt": "pt-br", "es": "es", "fr": "fr-fr",
                "it": "it", "hi": "hi", "ja": "ja", "zh": "zh"}
_KOKORO_BY_LANG = {  # non-English: keep the bot's gender, switch to a native voice
    "pt": {"f": "pf_dora", "m": "pm_alex"},
    "es": {"f": "ef_dora", "m": "em_alex"},
    "fr": {"f": "ff_siwis", "m": "ff_siwis"},
    "it": {"f": "if_sara", "m": "im_nicola"},
    "hi": {"f": "hf_alpha", "m": "hm_omega"},
    "ja": {"f": "jf_alpha", "m": "jm_kumo"},
    "zh": {"f": "zf_xiaoxiao", "m": "zm_yunjian"},
}
_kokoro = None


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        if not KOKORO_ONNX.is_file() or not KOKORO_VOICES_FILE.is_file():
            raise FileNotFoundError(f"Kokoro model missing in {KOKORO_DIR} — run ./fetch-kokoro.sh")
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(str(KOKORO_ONNX), str(KOKORO_VOICES_FILE))
        log.info("Kokoro loaded from %s", KOKORO_DIR)
    return _kokoro


def _resolve_voice(name: str, lang_iso: str) -> tuple[str, str]:
    """(kokoro voice, kokoro lang) for the text's language, preserving the bot's gender."""
    gender = "f" if len(name) > 1 and name[1] == "f" else "m"
    if lang_iso == "en":
        return name, ("en-gb" if name[:1] == "b" else "en-us")
    native = _KOKORO_BY_LANG.get(lang_iso)
    if native:
        return native[gender], _KOKORO_LANG[lang_iso]
    return name, "en-us"  # unknown language: best-effort with the bot's own voice


def synthesize_voice(text: str, voice: dict | None = None) -> str | None:
    """Blocking: turn text into a Telegram-ready ogg/opus voice file; return its path
    (or None on failure). Kokoro (offline, local ONNX model) -> wav -> ffmpeg -> ogg/opus.
    `voice` (per-bot config): name (Kokoro voice), speed, plus optional pitch/robot ffmpeg."""
    text = " ".join(text.split())
    if not text:
        return None
    voice = voice or {}
    try:
        import soundfile as sf
        VOICE_TMP.mkdir(parents=True, exist_ok=True)
        stem = VOICE_TMP / uuid.uuid4().hex
        wav, ogg = f"{stem}.wav", f"{stem}.ogg"
        lang_iso = detect_tts_lang(text[:4000], default=os.environ.get("WHISPER_LANGUAGE") or "en")
        name, klang = _resolve_voice(voice.get("name", DEFAULT_VOICE), lang_iso)
        speed = float(voice.get("speed", 1.0))
        log.info("voiceback: kokoro voice=%s lang=%s speed=%s robot=%s (%d chars)",
                 name, klang, speed, bool(voice.get("robot")), len(text))
        samples, sr = _get_kokoro().create(text[:4000], voice=name, speed=speed, lang=klang)
        sf.write(wav, samples, sr)
        af = _voice_filters(voice)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", wav]
        if af != "aresample=48000":
            cmd += ["-af", af]
        # -ar 48000 is REQUIRED: Kokoro is 24kHz and Telegram reads the OggOpus input-sample-rate
        # header for playback timing — a 24000 stamp makes every voice play ~2x too fast.
        cmd += ["-ar", "48000", "-ac", "1", "-c:a", "libopus", "-b:a", "48k", ogg]
        subprocess.run(cmd, check=True, timeout=180)
        try:
            os.remove(wav)
        except OSError:
            pass
        return ogg
    except Exception:
        log.exception("Voice synthesis failed")
        return None


# Voiceback is a persistent mode toggled by `bot voice on`/`off` (there is no per-message
# trigger — that was finicky over transcription). While on, every reply comes back as audio.
VOICE_MODE_FILE = HERE / "voice.mode"  # presence = persistent "spoken replies for everything"


def voice_mode_on() -> bool:
    return VOICE_MODE_FILE.exists()


def set_voice_mode(on: bool) -> None:
    if on:
        VOICE_MODE_FILE.write_text("on", encoding="utf-8")
    else:
        try:
            VOICE_MODE_FILE.unlink()
        except OSError:
            pass


# The anti-stall guard is a persistent toggle (like voiceback): while on, the guard bot reviews
# any session that goes idle with nothing running and forces it back to work if it's stalling.
# Presence of the flag file = on. Default OFF (file absent).
def nostall_on() -> bool:
    return NOSTALL_FILE.exists()


def set_nostall(on: bool) -> None:
    if on:
        NOSTALL_FILE.write_text("on", encoding="utf-8")
    else:
        try:
            NOSTALL_FILE.unlink()
        except OSError:
            pass


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


INTRUSION_OFF_FILE = HERE / "INTRUSION_OFF.flag"  # presence = paranoid gate DISABLED (default ON)


def intrusion_gate_on() -> bool:
    """Paranoid intrusion gate: ON by default. The GUI toggle creates INTRUSION_OFF_FILE to
    disable it — at the machine only, never via a remote command (same principle as the
    physical-unlock-only hard-lock)."""
    return not INTRUSION_OFF_FILE.exists()


async def handle_intrusion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An unauthorized user touched the bot → treat it as an intrusion: log it, HARD-LOCK the
    bridge (kill Claude + engage the block flag → physical-unlock-only at the tray), and alert
    the owner on Telegram. Idempotent: if already locked, just log (no re-kill / no alert
    spam). The intruder gets no reply."""
    user = update.effective_user
    uid = user.id if user else "?"
    name = user.full_name if user else "?"
    uname = f" @{user.username}" if (user and user.username) else ""
    m = update.effective_message
    content = ""
    if m is not None:
        content = (m.text or m.caption
                   or ("<voice>" if m.voice else "")
                   or ("<photo>" if m.photo else "")
                   or "<message>")
    log.warning("🚨 INTRUSION: unauthorized id=%s name=%r%s content=%r",
                uid, name, uname, _oneline(content, 200))
    if not intrusion_gate_on():
        return  # paranoid gate toggled OFF at the machine — logged only, no lock
    if is_blocked():
        return  # already locked — don't re-kill or re-alert
    reason = f"intrusion: unauthorized telegram id {uid} ({name}{uname})"
    for s in list(registry.sessions.values()):  # kill EVERY session, not just current
        try:
            await s.controller.kill()
        except Exception:
            log.exception("intrusion: kill failed for %s", s.name)
    engage_block(reason)
    for owner in ALLOWED_USER_IDS:  # alert the owner — the bot can still send while locked
        try:
            await context.bot.send_message(
                owner,
                "🚨 LOCKED — someone who isn't you tried to use the bot.\n"
                f"From: id {uid} {name}{uname}\n"
                f"Sent: “{_oneline(content, 200)}”\n\n"
                "Claude was killed and the bridge is hard-locked. "
                "Unlock at the tray on the machine."
            )
            mark_sent()
        except Exception:
            log.exception("intrusion: owner alert failed")


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
        await handle_intrusion(update, context)
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
        await handle_intrusion(update, context)
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

    # Which session receives this? The CURRENT one AT SEND TIME (captured now, so a `bot select`
    # during a long decode can't misroute it). badge = "" unless multiplexing.
    target = registry.current()
    badge = registry.badge(target)

    # Serialize transcription: ONE decode at a time, in MESSAGE order. Concurrent updates would
    # otherwise spawn parallel decoders — a short later clip finishes first (out of order), and
    # N whisper processes thrash the CPU. Acquire the lock BEFORE downloading so ordering follows
    # message order, not download-completion order; show "queued" while a previous decode runs.
    queued = _transcribe_lock.locked()
    prog = await context.bot.send_message(
        msg.chat_id,
        badge + ("🎙 Queued — waiting for the current transcription…" if queued else "🎙 Transcribing…"),
        reply_to_message_id=msg.message_id)
    mark_sent()

    # Download to a dedicated temp dir, transcribe, then DELETE the audio right away.
    AUDIO_TMP.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".oga" if msg.voice else ".bin", dir=AUDIO_TMP, delete=False
    ) as tmp:
        tmp_path = tmp.name
    await _transcribe_lock.acquire()
    try:
        if queued:  # our turn now — flip the bubble from "queued" to "transcribing"
            try:
                await context.bot.edit_message_text(
                    badge + "🎙 Transcribing…", chat_id=msg.chat_id, message_id=prog.message_id)
            except Exception:
                pass
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(tmp_path)

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
                    text = (f"{badge}🕐 {time.strftime('%H:%M:%S')} · 🎙 Transcribing…"
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
        transcribe_begin()  # tell the idle watchdog we're decoding (freeze its ×N counters)
        try:
            compute = session_compute(target)
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
            transcribe_end()
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
        _transcribe_lock.release()
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
    await reply_chunked(msg, badge + f"🗣 {text}")
    # "bot ..." messages are harness commands and never reach Claude.
    if await maybe_handle_bot_command(context, msg.chat_id, msg.message_id, text):
        return
    if is_blocked():
        await msg.reply_text(BLOCKED_MSG)
        return
    set_no_more_work(target, False)  # new work from the user re-arms the idle nudger
    target.parked = False            # ...and un-parks it
    enqueue_for_claude(target, msg.chat_id, msg.message_id, text, "audio", False)


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

    def __init__(self, bot, chat_id, reply_to, prefix: str = ""):
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.prefix = prefix  # color-bubble badge prepended to EACH message (multiplex only)
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
            piece = self.prefix + text[i:i + self.TG_LIMIT]
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
        await self.bot.send_message(self.chat_id, self.prefix + "[[END]]")
        mark_sent()
        log.info("TG [[END]] sent in %.2fs", time.monotonic() - t0)


class SegmentRenderer:
    """Renders ONE Claude turn (a 'segment') to the chat: a live activity board while
    it works, the answer streamed below, a summary at the end. Used for BOTH user-driven
    turns and the turns Claude starts on its own when a background shell lands."""

    def __init__(self, bot, chat_id, reply_to, header, *, user_text=None, voiceback=False,
                 badge="", controller=None, session=None):
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.base_header = header
        self.session = session      # owning Session (for per-session NO MORE WORK)
        self.badge = badge          # color-bubble tag ("" unless multiplexing)
        self.ctrl = controller or globals()["controller"]  # this turn's session controller
        self.user_text = user_text or "(self-initiated turn)"
        self.voiceback = voiceback  # spoken reply: no live streaming, TTS at the end
        self.board = StatusBoard(bot, chat_id, reply_to, header)
        self.streamer = ParagraphStreamer(bot, chat_id, reply_to, prefix=badge)
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
        """A real bottom-of-chat message (notifies), for genuine failures/summaries. Badged
        with the session's color bubble so alerts self-identify in an interleaved scroll."""
        try:
            await self.bot.send_message(self.chat_id, self.badge + text)
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
            await self.ctrl.interrupt()
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
                    # it as one voice message at finalize.
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
                        issue = f"{name or 'tool'}: {_oneline(_blocktext(block.content), 120)}"
                        self.problems.append(issue)
                        record_issue(issue)
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
        # Autonomy: if Claude's reply LEADS WITH the marker it's declaring it's out of work
        # -> pause idle nudging (re-armed on the user's next message). Done BEFORE the
        # voiceback branch (under voiceback the answer is collected, not streamed), startswith
        # exactly as the nudge prompt instructs Claude.
        answer = "".join(self.answer_buf).strip() or final
        if answer and self.session is not None:
            self.session.recent_answers.append(answer)  # what the guard reviews if this bot stalls
            self.session.nostall_cleared = False        # new output — let the guard review again
        clean = answer.lstrip()
        if NO_MORE_WORK_MARKER in clean.upper():
            set_no_more_work(self.session, True)
            log.info("watchdog: Claude declared NO MORE WORK — idle nudging paused")
        if self.voiceback:
            await self._finalize_voiceback(res, final)
            return
        # Only feed `final` if nothing streamed (else it double-sends the answer).
        if not self.answer_buf and final:
            await self.streamer.feed(final)
        await self.streamer.finish()  # remainder + [[END]] (prompt is free for input)

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
                 res.get("_ctx_str", ""), self.ctrl.session_id)

    async def _compute_ctx(self, res: dict) -> None:
        ctx = await self.ctrl.context_usage()
        res["_ctx_str"] = f" · ctx {ctx['percentage']:.0f}%" if ctx else ""

    def _summary(self, res: dict) -> str:
        ctx = res.get("_ctx_str", "")
        usage = format_usage()  # ` · 5h N% (⟳..) · wk N% (⟳..)` from the cached scrape
        turns, secs = res.get("turns", "?"), res.get("secs", 0)
        sid8 = (self.ctrl.session_id or "")[:8]
        sess = f" · 🧵 {sid8}" if sid8 else ""
        probs = f" · ⚠️ {len(self.problems)} issue(s)" if self.problems else ""
        if res.get("is_error"):
            return f"⚠️ Ended: {res.get('subtype')} · {turns} turns · {secs:.0f}s{ctx}{usage}{sess}"
        return f"✅ Done · {turns} turns · {secs:.0f}s{ctx}{usage}{sess}{probs}"

    async def _finalize_voiceback(self, res: dict, final: str) -> None:
        """Spoken reply: freeze the board and send ONE voice message — the whole answer as a
        single audio, no text transcript alongside it."""
        spoken = ("".join(self.answer_buf).strip() or final).strip()
        log.info("ANSWER (voiceback, %d chars):\n%s", len(spoken), spoken)

        await self.board.seal(self.base_header + " · 🔊 voiceback 👇")
        sent_audio = False
        if spoken:
            voice = self.session.config.get("voice") if self.session else None
            ogg = await asyncio.to_thread(synthesize_voice, spoken, voice)
            if ogg:
                try:
                    with open(ogg, "rb") as fh:
                        await self.bot.send_voice(self.chat_id, voice=fh,
                                                  reply_to_message_id=self.reply_to)
                    mark_sent()
                    sent_audio = True
                    log.info("voiceback sent one audio (%d chars)", len(spoken))
                except Exception:
                    log.exception("send_voice failed")
                finally:
                    try:
                        os.remove(ogg)
                    except OSError:
                        pass
        if not sent_audio:
            await self.alert("🔇 (voiceback was on, but nothing could be spoken)")
        await self.bot.send_message(self.chat_id, self.badge + "[[END]]")
        mark_sent()
        await self._compute_ctx(res)
        await self.alert(self._summary(res) + (" · 🔊 audio" if sent_audio else ""))
        log.info("TURN DONE (voiceback): subtype=%s session=%s",
                 res.get("subtype"), self.ctrl.session_id)

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

    async def interrupted_close(self) -> None:
        """Close a turn the user ended with `bot interrupt`: capture whatever streamed, seal the
        board, and free the prompt ([[END]]) — with NO crash/error notice. The interrupt's real
        ResultMessage is `is_error=True` (subtype error_during_execution), but that's not a crash:
        the command handler already told the user, and background shells keep running."""
        if self.tripped:
            return
        answer = "".join(self.answer_buf).strip()
        if answer and self.session is not None:
            self.session.recent_answers.append(answer)
            self.session.nostall_cleared = False
        if not self.board.sealed:
            try:
                await self.board.finish("⏸ interrupted")
            except Exception:
                pass
        try:
            await self.streamer.finish()   # flush remainder + [[END]] (prompt free for input)
        except Exception:
            log.exception("interrupted_close: streamer finish failed")

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
    context, session, chat_id, reply_to, user_text: str, source: str,
    raw: bool = False, header: str = "🤖 Claude is working…", voiceback: bool = False,
) -> None:
    """Drive ONE user turn for a SPECIFIC session. The continuous reader feeds messages to
    the renderer; we return when this turn ends. Claude's later self-started turns (a shell
    landed) are rendered by that session's SpontaneousRelay, so they reach the phone."""
    bot = context.bot
    ctrl = session.controller
    badge = registry.badge(session)
    if session.config.get("voiceback") is False:
        voiceback = False  # this bot opts out of voiceback (e.g. an image-only bot)
    log.info("DISPATCH[%s] start chat=%s source=%s busy=%s voiceback=%s len=%d",
             session.name, chat_id, source, ctrl.busy, voiceback, len(user_text))
    if ctrl.busy:
        await bot.send_message(chat_id, badge + "⏳ Still on the previous request — queuing this one.")
    if voiceback:
        header += " · 🔊 voiceback"
    header = badge + header  # board / seal-header carry the color bubble under multiplexing
    prompt = user_text if raw else build_prompt(user_text, source, voiceback=voiceback,
                                                bot_name=session.name)

    # Retry loop for Anthropic-side throttling (overloaded / 429): report + wait + retry.
    # A rate-limit is recognized ONLY from the structured RateLimitEvent (r.rate_limited)
    # or an ipsis-literis match on the EXCEPTION / error result — never a successful answer.
    attempt = 0
    while True:
        attempt += 1
        r = SegmentRenderer(bot, chat_id, reply_to, header, user_text=user_text,
                            voiceback=voiceback, badge=badge, controller=ctrl, session=session)
        await r.start()
        err = None
        try:
            await ctrl.ask(prompt, r.handle, on_system=r.on_system)
        except Exception as e:
            log.exception("Claude turn failed")
            err = f"{type(e).__name__}: {e}"
        else:
            res = r.result or {}
            if ctrl.consume_interrupt_flag():
                # `bot interrupt`: the turn ended by request (its ResultMessage is is_error, but
                # that's not a crash) — close cleanly; the command already told the user.
                await r.interrupted_close()
                return
            if res.get("is_error"):
                err = res.get("text") or f"ended: {res.get('subtype')}"
                log.warning("turn errored: subtype=%s rate_event=%s text=%s",
                            res.get("subtype"), r.rate_limited, _oneline(err, 200))
            else:
                await r.finalize()
                return

        # A user interrupt can also surface via the exception path — still a clean stop, not a crash.
        if ctrl.consume_interrupt_flag():
            await r.interrupted_close()
            return
        # An error occurred. Is it throttling? (clear marker OR verbatim — not the answer.)
        throttled = r.rate_limited or is_rate_limited(err)
        if throttled and attempt <= RATE_LIMIT_MAX_RETRIES:
            await r.rate_limited_notice(attempt, RATE_LIMIT_MAX_RETRIES, RATE_LIMIT_RETRY_SECS // 60)
            await asyncio.sleep(RATE_LIMIT_RETRY_SECS)
            continue
        if session.empty_reply:
            log.info("DISPATCH[%s] turn failed (model unavailable on subscription?) — empty_reply: %s",
                     session.name, _oneline(err, 200))
            await r.board.finish(badge + session.empty_reply)
            return
        await r.crashed_text(err)
        return


def discover_bots() -> dict:
    """Scan `bots/*/` → {name: config}, the entire bot roster. A bot exists on disk if its
    directory has a config.json or a main.md; its icon, aliases, and internal flag all live in
    that config. Adding or removing a bot is a filesystem operation, not a code change."""
    found = {}
    if BOTS_DIR.is_dir():
        for d in sorted(BOTS_DIR.iterdir()):
            if d.is_dir() and ((d / "config.json").is_file() or (d / "main.md").is_file()):
                found[d.name] = bot_config(d.name)
    return found


def canonical_bot() -> str:
    """The session loaded on startup and in single-bot mode: DEFAULT_BOT (from .env) if it
    names a real bot on disk, else the always-regenerated 'claude' default — so an unset,
    blank, or dangling DEFAULT_BOT safely falls back to 'claude'."""
    name = os.environ.get("DEFAULT_BOT", "").strip()
    return name if (name and name != DEFAULT_SESSION and name in discover_bots()) else DEFAULT_SESSION


def bot_icon(name: str) -> str:
    """A bot's badge emoji — from its own config; the default gear if it declares none."""
    return bot_config(name).get("icon") or DEFAULT_ICON


def selectable_bots() -> list:
    """Names a user can `bot select`: the scanned roster minus internal bots, default first."""
    names = sorted(n for n, c in discover_bots().items() if not c.get("internal"))
    if DEFAULT_SESSION in names:
        names.remove(DEFAULT_SESSION)
        names.insert(0, DEFAULT_SESSION)
    return names


def session_aliases() -> dict:
    """Fuzzy voice/text aliases → canonical name, assembled from each bot's config `aliases`."""
    out = {}
    for name, cfg in discover_bots().items():
        for a in cfg.get("aliases") or []:
            out[str(a).strip().lower()] = name
    return out


def nostall_bot_available() -> bool:
    """The guard can only run if its bot is actually installed (bots/<NOSTALL_BOT>/). bot.py
    knows the name; the bot supplies its own icon/model/effort via config."""
    return NOSTALL_BOT in discover_bots()


def ensure_nostall_bot():
    """Ensure the guard's bot exists in the registry when the guard is on. It's driven directly
    via `ask_text` — NO queue worker, watchdog, or relay — so it never auto-ends, never polices
    itself, and never flips multiplexing. Its Claude connects lazily on the first consult (session
    id persisted like any other bot). Returns the Session, or None if the guard is off / the bot
    isn't installed. Idempotent."""
    if not nostall_on() or not nostall_bot_available():
        return None
    s = registry.sessions.get(NOSTALL_BOT)
    if s is None:
        s = Session(NOSTALL_BOT)
        registry.sessions[NOSTALL_BOT] = s
        log.info("nostall: %s activated (%s)", NOSTALL_BOT, _model_label(s.controller))
    return s


async def ask_text(session, prompt: str, timeout: float = 240) -> str:
    """Run ONE quiet turn on a session's controller and return its final answer text — no
    channel rendering, no board. Used to consult the guard bot. Best-effort: returns '' on any
    error/timeout so a stalled consult can never wedge the caller (the watchdog)."""
    box = {"text": ""}

    async def sink(msg):
        if isinstance(msg, ResultMessage):
            box["text"] = (getattr(msg, "result", "") or "").strip()

    try:
        await asyncio.wait_for(session.controller.ask(prompt, sink), timeout)
    except Exception:
        log.exception("ask_text failed for %s", session.name)
    return box["text"]


class SpontaneousRelay:
    """Renders the turns Claude starts on its OWN (a background shell completed) to the
    owner's chat — a fresh segment each time, posted at the bottom. This is what makes
    'I'll report when the build lands' actually reach your phone."""

    def __init__(self, application, session):
        self.app = application
        self.session = session
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
            badge = registry.badge(self.session)
            self.cur = SegmentRenderer(
                self.app.bot, chat, None,
                badge + "🔔 Claude picked back up (a background task landed)…",
                badge=badge, controller=self.session.controller, session=self.session,
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

    def __init__(self, app, session):
        self.app = app
        self.session = session    # the session whose Claude this watchdog monitors
        self.msg_id = None
        self.body = None          # dedupe key: status text without stamp/counter
        self.count = 1
        self.is_latest = False    # is our status message still the newest in the chat?
        self.done_declared = False  # IDLE_DONE one-shot guard (Claude said NO MORE WORK)
        self._nostall_last = 0.0    # monotonic ts of the last anti-stall intervention (cooldown)
        self._police_task = None    # in-flight anti-stall consult (kept OFF the loop's critical path)

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
                if transcribe_active():
                    # Busy decoding voice (not a Claude turn, so status() reads "idle"):
                    # freeze every ×N counter and skip nudging. The transcription bubble is
                    # already showing liveness, so the watchdog stays quiet until it's done.
                    continue
                st = self.session.controller.status()
                idle_no_shells = (not st["active"]) and (not st["shells"])
                if self.session.parked:
                    # Parked (bot park): intentional end-state idle. Never auto-ended, nudged, or
                    # policed by the anti-stall guard. Announce once when it settles, then stay
                    # silent until the user sends it something (which un-parks it).
                    if idle_no_shells and not self.done_declared:
                        self.done_declared = True
                        await self._show("🅿️ parked — end-state idle, no nudging or anti-stall. "
                                         "Send anything to wake it.")
                    continue
                background = (self.session.name != DEFAULT_SESSION
                             and registry.current() is not self.session)
                if idle_no_shells and background:
                    # A background session (not selected, not default) with nothing running:
                    # auto-end it to free resources — re-selecting recreates it (session id kept).
                    # If it declared NO MORE WORK, end promptly; else after a stretch of idle.
                    await self._show("💤 idle · 🐚 no shells — nothing running.")
                    if is_no_more_work(self.session) or self.count >= IDLE_AUTOEND_AT:
                        log.info("watchdog[%s]: background + nothing running -> auto end",
                                 self.session.name)
                        try:
                            await self.app.bot.send_message(
                                self._chat(), registry.badge(self.session)
                                + "⏹ auto-ended (idle, nothing running, not the selected bot).")
                            mark_sent()
                        except Exception:
                            log.exception("auto-end notice failed")
                        asyncio.create_task(end_session(self.session.name))
                        return
                    continue
                policing = nostall_on() and not self.session.internal
                if idle_no_shells and is_no_more_work(self.session):
                    # IDLE_DONE: Claude declared it's out of work. One-shot + terminal — no
                    # nudging — until the user sends something (which clears the flag). Input
                    # is NEVER gated by this; it only silences the auto-nudge.
                    if self.done_declared:
                        continue
                    self.done_declared = True
                    if policing:
                        # Anti-stall on: don't just accept "done" — let the guard second-guess it,
                        # OFF the watchdog's critical path (a slow guard must not freeze the loop).
                        # The guard posts its own verdict — a stand-down notice if it agrees, or a
                        # kick if not — so the watchdog stays out of the terminal messaging here.
                        self._spawn_police("it declared NO MORE WORK")
                        continue
                    await self._show("✅ idle · done — you said NO MORE WORK. "
                                     "Say hi whenever there's more.")
                    continue
                self.done_declared = False
                if idle_no_shells:
                    # IDLE_NO_SHELLS: accumulate ×N (refreshing datetime). With the anti-stall
                    # guard ON, it reviews the bot AS SOON AS this status appears (the first idle+
                    # no-shells tick) — `_police_stall`'s own cooldown stops it from re-reviewing
                    # too fast. With the guard OFF, Claude gets the canned nudge only at ×30.
                    await self._show("💤 idle · 🐚 no shells — nothing running.")
                    if policing and not self.session.nostall_cleared:
                        self._spawn_police("it's idle with nothing running")
                    elif self.count == IDLE_NO_SHELLS_NUDGE_AT:
                        await self._nudge_idle_no_shells()
                    continue
                idle_with_shells = (not st["active"]) and bool(st["shells"])
                await self._show(self._status_body(st))
                # Idle with shells for a long stretch (×N) -> nudge Claude to act.
                if idle_with_shells and self.count == IDLE_SHELLS_NUDGE_AT:
                    await self._nudge_idle_shells()
            except Exception:
                log.exception("watchdog error")

    def _spawn_police(self, reason: str) -> None:
        """Run the anti-stall consult OFF the watchdog's critical path. The guard's review can
        take a minute (a slow model turn); awaiting it inline would freeze THIS bot's watchdog —
        no status refresh, no silence tracking — for the whole window, which reads as a dead
        watchdog. So fire it as a task and return immediately; the loop keeps ticking. At most one
        consult in flight per bot (a second tick while one runs is a no-op); `_police_stall`'s own
        cooldown throttles back-to-back consults and posts the 'reviewing…' one-liner once it
        commits. Its return value is now advisory — the guard owns all of its own messaging."""
        if self._police_task is not None and not self._police_task.done():
            return
        async def _run():
            try:
                await self._police_stall(reason)
            except Exception:
                log.exception("nostall: police task crashed")
        self._police_task = asyncio.create_task(_run())

    async def _police_stall(self, reason: str) -> bool:
        """Anti-stall: hand this bot's most-recent answers to the guard bot and let it judge
        whether the stop is genuine or a stall. Returns True if the guard rules it a legitimate
        stop, False if the guard browbeat it (already posted + re-queued as the bot's next turn)
        OR the consult was skipped. Cooldown-throttled per bot so a stubborn bot can't spin the
        guard in a tight loop. Only ever called idle + no shells, and always via `_spawn_police`
        (off the watchdog's critical path)."""
        now = time.monotonic()
        if (now - self._nostall_last) < NOSTALL_COOLDOWN_SECS:
            return False
        chat = self._chat()
        recent = [a for a in list(self.session.recent_answers) if a.strip()]
        if chat is None or not recent:
            return False
        guard = ensure_nostall_bot()
        if guard is None or guard is self.session:
            return False
        self._nostall_last = now
        # Tell the OWNER (not the reviewed bot) that the guard is on it. The review can take a
        # minute, so this one-liner shows the reasoning window as activity instead of a frozen
        # watchdog; the verdict (stand-down or kick) lands below it when the guard finishes.
        try:
            await self.app.bot.send_message(
                chat, registry.badge(self.session) + "🐕 anti-stall: reviewing whether it's really done…")
            mark_sent()
        except Exception:
            log.exception("nostall: posting the reviewing notice failed")

        def _cap(a: str) -> str:
            return a if len(a) <= 2000 else a[:2000] + " …[truncated]"

        convo = "\n\n--- next message ---\n\n".join(_cap(a) for a in recent)
        # Pure plumbing: hand the guard the raw material and let its main.md / var/ define the whole
        # policing protocol (what stalling is, how to argue, and the LEGIT STOP output contract).
        prompt = (
            bot_boot_pointer(guard.name)
            + f'The bot "{self.session.name}" just stopped with nothing running ({reason}). '
            "Its most recent messages, oldest first:\n\n"
            f"{convo}"
        )
        log.warning("nostall: policing %s (%s)", self.session.name, reason)
        verdict = (await ask_text(guard, prompt)).strip()
        if not verdict:
            return False
        if verdict.upper().startswith(NOSTALL_LEGIT_MARKER):
            log.info("nostall: cleared %s — genuinely done", self.session.name)
            self.session.nostall_cleared = True  # one-shot: don't re-review until it works again
            reason = verdict[len(NOSTALL_LEGIT_MARKER):].lstrip(" .:—–-").strip()
            note = ("🐕 anti-stall: reviewed it and it's genuinely done — standing down."
                    if not reason else f"🐕 anti-stall: it's genuinely done — {reason}")
            try:
                await self.app.bot.send_message(chat, registry.badge(self.session) + note)
                mark_sent()
            except Exception:
                log.exception("nostall: posting the done notice failed")
            return True
        # Stalling: show the intervention (in the watchdog's own voice) and kick the bot back.
        log.warning("nostall: %s was stalling — kicking it back", self.session.name)
        try:
            await self.app.bot.send_message(
                chat, registry.badge(self.session) + "🐕 caught it stalling — back to work 👇")
            await reply_chunked_bot(self.app.bot, chat, None, verdict)
            mark_sent()
        except Exception:
            log.exception("nostall: posting the intervention failed")
        set_no_more_work(self.session, False)
        # The bot never converses with the guard — it just receives the order and acts on it.
        # Inject as a bare anti-stall directive (no persona to reply to) so it resumes.
        enqueue_for_claude(self.session, chat, None,
                           "[anti-stall] " + verdict, "text", False)
        return False

    async def _nudge_idle_shells(self):
        """~30 min idle with shells still running: ask Claude to continue, check for stuck
        shells, or clean up. Goes through the normal queue so the reply reaches the phone."""
        log.warning("watchdog[%s]: idle+shells ×%d — auto-nudging Claude",
                    self.session.name, IDLE_SHELLS_NUDGE_AT)
        chat = self._chat()
        if chat is None:
            return
        try:
            await self.app.bot.send_message(
                chat, registry.badge(self.session) + "🐕 Idle a long time with shells still "
                "running — nudging Claude to continue, check for stuck shells, or clean up.")
            mark_sent()
        except Exception:
            log.exception("nudge notice failed")
        enqueue_for_claude(self.session, chat, None, IDLE_SHELLS_NUDGE, "text", False)

    async def _nudge_idle_no_shells(self):
        """~30 min idle with NOTHING running: ask Claude to continue or declare done (reply
        leading with NO MORE WORK). Goes through the normal queue so the reply reaches the
        phone; if Claude declares done, finalize() pauses these nudges until you send work."""
        log.warning("watchdog[%s]: idle+no-shells ×%d — nudging Claude (continue or NO MORE WORK)",
                    self.session.name, IDLE_NO_SHELLS_NUDGE_AT)
        chat = self._chat()
        if chat is None:
            return
        try:
            await self.app.bot.send_message(
                chat, registry.badge(self.session) + "🐕 Idle a while with nothing running — "
                "nudging Claude to continue or declare it's out of work.")
            mark_sent()
        except Exception:
            log.exception("idle-no-shells nudge notice failed")
        enqueue_for_claude(self.session, chat, None, IDLE_NO_SHELLS_NUDGE, "text", False)

    async def _show(self, body):
        chat = self._chat()
        if chat is None:
            return
        body = registry.badge(self.session) + body  # color-bubble tag under multiplexing
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
        # This session keeps editing its OWN balloon in place (×N). A watchdog balloon does
        # NOT bury other sessions' balloons — otherwise several idle sessions on the 60s poll
        # leapfrog and none accumulates. Only real content (mark_sent) buries all balloons.
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
    if norm in ("interrupt", "int"):
        return "interrupt"
    if norm in ("stop", "cancel", "abort", "halt", "stop it"):
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
    "• bot interrupt / bot int — Esc/Ctrl-C: stop the current turn, KEEP background shells running\n"
    "• bot stop — interrupt + reset the connection (drops background work); session resumes\n"
    "• bot drop — discard messages queued but not yet sent to Claude\n"
    "• bot kill — force-kill the Claude process (kill -9), then respawn\n"
    "• bot lock — kill Claude AND lock the bridge (unlock at the machine)\n"
    "• bot sleep — pause Telegram input (Claude keeps running); wake at the machine\n"
    "• bot effort [level] — show/set reasoning effort (low|medium|high|xhigh|max)\n"
    "• bot cwd [path] — show/set THIS bot's working directory (each bot is independent)\n"
    "• bot transcribe [best|good|fast] — show/set voice transcription quality\n"
    "• bot context — detailed context-window usage\n"
    "• bot logs [n] — last n bridge log lines\n"
    "• bot issues — list & clear recent tool errors\n"
    "• bot restart — restart the bridge process\n"
    "• bot echo <text> — echo text back (not sent to Claude)\n"
    "• bot voice [on|off] — spoken replies for EVERYTHING (toggle)\n"
    "• bot nostall [on|off] — anti-stall guard: forces idle bots back to work if they stall\n"
    "• bot park — force THIS bot into intentional end-state idle (no nudging, no anti-stall)\n"
    "• bot harness <text> (or bot h) — message the AI working on this machine\n"
    "• bot status — bridge, effort, session & context\n"
    "• bot session — current session id\n"
    "• bot sessions — list parallel sessions (multiplexing)\n"
    "• bot select <name> — switch to / create a parallel Claude (names: bot sessions)\n"
    "• bot end <name> — tear down a parallel session\n"
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


VALID_MODELS = {"opus", "sonnet", "haiku", "opusplan", "fable"}
MODEL_RESET = {"default", "none", "reset", "auto"}      # config-only (never a live command)


def default_model():
    """EFFECTIVE default for sessions with no forced model — the driver's fable
    guard applied over the ambient env/settings default, so the label matches
    what actually runs."""
    return default_model_guard() or ambient_default_model()


def _model_label(ctrl) -> str:
    if ctrl.forced_model:
        return ctrl.forced_model
    actual = ctrl.model or default_model()
    return f"default ({actual})" if actual else "default"


async def _status_text() -> str:
    cur = registry.current()
    ctrl = cur.controller
    busy = "working" if ctrl.busy else "idle"
    sid = ctrl.session_id
    ctx = await ctrl.context_usage()
    sc = session_compute(cur)
    lines = []
    if registry.multiplexing():
        lines.append(f"🤖 current bot: {cur.emoji} {cur.name} ({busy})")
    else:
        lines.append(f"🤖 Claude: {busy}")
    lines.append(f"🧠 model: {_model_label(ctrl)}")
    lines.append(f"⚙️ effort: {ctrl.get_effort() or 'default'}")
    lines.append(f"🧵 session: {(sid[:8] + '…') if sid else 'new (none yet)'}")
    if ctx:
        lines.append(f"📊 context: {ctx['percentage']:.0f}%")
    lines.append(f"📂 cwd: {ctrl.get_cwd()}")
    lines.append(f"🎙 transcribe: {MODEL_SIZE}/{sc} ({_PRESET_BY_COMPUTE.get(sc, '?')})")
    lines.append(f"🔊 voice replies: {'on' if voice_mode_on() else 'off'}")
    if nostall_on():
        lines.append("🐕 anti-stall: on")
    if cur.parked:
        lines.append("🅿️ parked (end-state idle)")
    if registry.multiplexing():
        others = " ".join(f"{s.emoji}{s.name}" for s in registry.sessions.values()
                          if not s.internal)
        lines.append(f"🗂 sessions: {others}")
    if is_blocked():
        lines.append("🔒 LOCKED — unblock at the machine to resume")
    if is_sleeping():
        lines.append("😴 SLEEPING — input paused; WAKE UP on the tray to resume")
    return "\n".join(lines)


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


def _sessions_overview() -> str:
    """Human list of all live sessions (for `bot sessions` / `bot select` with no name)."""
    lines = []
    for name, s in registry.sessions.items():
        if s.internal:
            continue  # internal/system bots are not part of the user-facing session list
        cur = " ← current" if name == registry.current_name else ""
        busy = "working" if s.controller.busy else "idle"
        sid = (s.controller.session_id or "")[:8] or "new"
        mdl = _model_label(s.controller)
        lines.append(f"{s.emoji} {s.name} · {busy} · {mdl} · 🧵 {sid} · 📂 {s.controller.get_cwd()}{cur}")
    body = "\n".join(lines)
    pal = ", ".join(f"{bot_icon(n)}{n}" for n in selectable_bots())
    if registry.multiplexing():
        body += f"\n\nSwitch: bot select <name> · End: bot end <name>\nNames: {pal}"
    else:
        body += ("\n\n(single session — `bot select <name>` switches to / creates a bot and "
                 f"turns on color-tagging; names: {pal})")
    return "🗂 Sessions:\n" + body


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

    # "bot model [name]" — show or override the model for this session (applies next turn).
    m = re.match(r"^model\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        raw = m.group(1).strip().strip(" .!?,;:")
        log.info("bot command: model %r", raw)
        if not raw:
            await reply(f"🧠 Model: {_model_label(controller)}\n"
                        "Set with: bot model <opus|sonnet|haiku|fable|default>")
        else:
            name = raw.lower()
            if name in MODEL_RESET:
                await controller.set_model(None)
                await reply("🧠 Model → default (applies going forward).")
            elif name in VALID_MODELS or name.startswith("claude-"):
                name = MODEL_ALIASES.get(name, name)
                await controller.set_model(name)
                await reply(f"🧠 Model set to: {name} (applies going forward).")
            else:
                await reply(f'🧠 Unknown model "{raw}". Try: opus, sonnet, haiku, fable, or default.')
        return True

    # "bot cwd [path]" / "bot pwd" — show or set Claude's working directory.
    m = re.match(r"^(cwd|chdir|workdir|pwd)\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        verb, target = m.group(1).lower(), m.group(2).strip().strip(" '\"")
        if verb == "pwd" or not target:
            hint = "" if verb == "pwd" else "\nSet with: bot cwd <path>"
            await reply(f"📂 Working dir: {controller.get_cwd()}{hint}")
        elif await controller.set_cwd(target):
            cur = registry.current()
            who = f"{cur.emoji} {cur.name}: " if registry.multiplexing() else ""
            await reply(f"📂 {who}Working dir → {controller.get_cwd()} — "
                        "this bot's conversation moved here.")
        else:
            await reply(f"📂 Couldn't switch to: {target}")
        return True

    # "bot transcribe [best|good|fast]" — show or set THIS bot's transcription quality. Live for
    # the process (never persisted); takes effect on its NEXT voice message (the decoder reads
    # the compute type fresh each spawn). The current session's live value only.
    m = re.match(r"^(?:transcribe|transcription|quality|tx)\b\s*(.*)$",
                 rest.strip(), re.IGNORECASE)
    if m:
        raw = m.group(1).strip().strip(" .!?,;:").lower()
        log.info("bot command: transcribe %r", raw)
        cur_sess = registry.current()
        who = f"{cur_sess.emoji} {cur_sess.name}: " if registry.multiplexing() else ""
        cur = session_compute(cur_sess)
        cur_name = _PRESET_BY_COMPUTE.get(cur, cur)
        menu = ("best — float32, most accurate (slowest)\n"
                "good — int8_float32, ~2× faster, near-best\n"
                "fast — int8, ~3-4× faster, slight accuracy loss")
        if not raw:
            await reply(
                f"🎚 {who}Transcription quality: {cur_name} ({cur})\n{menu}\n"
                "Set with: bot transcribe <best|good|fast>"
            )
        elif raw in TRANSCRIBE_PRESETS:
            cur_sess.compute = TRANSCRIBE_PRESETS[raw]
            await reply(
                f"🎚 {who}Quality → {raw} ({TRANSCRIBE_PRESETS[raw]}) for this bot. "
                "Applies to its next voice message."
            )
        elif raw in _PRESET_BY_COMPUTE:  # they typed the raw compute type itself
            cur_sess.compute = raw
            await reply(
                f"🎚 {who}Quality → {_PRESET_BY_COMPUTE[raw]} ({raw}) for this bot. "
                "Applies to its next voice message."
            )
        else:
            await reply(f'🎚 Unknown quality "{raw}".\n{menu}')
        return True

    # "bot voice [on|off]" — persistent spoken-reply mode: while on, every answer comes back as
    # audio. Toggles when given no argument (also accepts the Portuguese "voz").
    m = re.match(r"^(?:voice|voz)\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        arg = m.group(1).strip().strip(" .!?,;:").lower()
        if arg in ("on", "yes"):
            state = True
        elif arg in ("off", "no", "stop"):
            state = False
        else:
            state = not voice_mode_on()  # no/unknown arg → toggle
        set_voice_mode(state)
        log.info("bot command: voice -> %s", "on" if state else "off")
        await reply(
            "🔊 Voice replies ON — every answer comes back as audio. Turn off with: bot voice off"
            if state else "🔇 Voice replies OFF — back to text."
        )
        return True

    # "bot nostall [on|off]" — the anti-stall guard. While ON, a background policing bot reviews
    # any bot that goes idle with nothing running and forces it back to work if it's stalling.
    # Global sticky like `bot voice`; toggles with no argument. Can't be turned on if its bot
    # isn't installed.
    m = re.match(r"^(?:nostall|antistall|anti-?stall|no-?stall)\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        arg = m.group(1).strip().strip(" .!?,;:").lower()
        if arg in ("on", "yes"):
            state = True
        elif arg in ("off", "no", "stop"):
            state = False
        else:
            state = not nostall_on()  # no/unknown arg → toggle
        if state and not nostall_bot_available():
            await reply(f"⚠️ Can't turn on anti-stall — the '{NOSTALL_BOT}' bot isn't installed "
                        f"(bots/{NOSTALL_BOT}/ is missing).")
            return True
        set_nostall(state)
        if state:
            ensure_nostall_bot()
        elif NOSTALL_BOT in registry.sessions:
            asyncio.create_task(end_session(NOSTALL_BOT))
        log.info("bot command: nostall -> %s", "on" if state else "off")
        await reply(
            "🐕 Anti-stalling guard is ON — a bot that stops with nothing running gets reviewed "
            "and forced back to work if it's stalling. Turn off with: bot nostall off"
            if state else "🐕 Anti-stalling guard is OFF."
        )
        return True

    # "bot park" — force the current bot into intentional end-state idle: the watchdog stops
    # nudging it AND the anti-stall guard leaves it alone. It's deliberate rest, not a stall.
    # Cleared automatically the moment you send it anything. Doesn't interrupt a running turn —
    # it takes effect once the bot next goes idle (use `bot stop` to halt active work first).
    if re.match(r"^park\b", rest.strip(), re.IGNORECASE):
        target = registry.current()
        target.parked = True
        log.info("bot command: park -> %s", target.name)
        who = f"{target.emoji} {target.name}" if registry.multiplexing() else "Claude"
        await reply(f"🅿️ Parked {who} — end-state idle, no nudging and no anti-stall policing. "
                    "Send it anything to wake it up.")
        return True

    # "bot logs [n]" — last N lines of the bridge log.
    m = re.match(r"^logs?\b\s*(\d*)$", rest.strip(), re.IGNORECASE)
    if m:
        n = max(1, min(int(m.group(1)), 60)) if m.group(1) else 20
        await reply("📜 last log lines:\n" + _tail_log(n))
        return True

    # "bot sessions" — overview of all live parallel sessions (multiplexing).
    if re.match(r"^sessions\b", rest.strip(), re.IGNORECASE):
        await reply(_sessions_overview())
        return True

    # "bot select|switch|use <name>" — switch to (creating on first use) a named parallel
    # session. No name → show the overview + palette. The 2nd session turns multiplexing ON
    # (from then on every bot artifact is color-tagged); back to just 'claude' turns it OFF.
    m = re.match(r"^(?:select|switch|use)\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        name = resolve_session_name(raw)
        if not raw:
            await reply(_sessions_overview())
        elif name is None:
            pal = ", ".join(f"{bot_icon(n)} {n}" for n in selectable_bots())
            await reply(f'🎨 Unknown session "{raw}". Pick one of: {pal}')
        else:
            existed = name in registry.sessions
            s = select_session(name)
            tag = "" if existed else " (new)"
            mux = (" · 🎨 multiplexing ON — replies are now color-tagged"
                   if registry.multiplexing() and not existed else "")
            log.info("bot command: select %r -> %s%s", raw, name, "" if existed else " (created)")
            await reply(f"{s.emoji} Now talking to {s.name}{tag}.{mux}\n" + await _status_text())
        return True

    # "bot end|close <name>" — tear down a named parallel session (never the default 'claude').
    m = re.match(r"^(?:end|close)\b\s*(.*)$", rest.strip(), re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        name = resolve_session_name(raw)
        if not raw:
            await reply("Usage: bot end <name> — tears down a parallel session (kills its Claude).")
        elif name is None:
            await reply(f'🗑 No session matching "{raw}".')
        else:
            status = await end_session(name)
            off = " (multiplexing off)" if not registry.multiplexing() else ""
            await reply(f"🗑 {status}.{off}")
        return True

    # "bot drop" — discard messages queued but not yet sent to Claude. Does NOT touch a turn
    # already running (that's `bot stop`); only clears the CURRENT session's waiting batch.
    if re.match(r"^drop\b", rest.strip(), re.IGNORECASE):
        dropped = drop_pending(registry.current())
        n = len(dropped)
        log.info("bot command: drop -> %d queued message(s)", n)
        if not n:
            await reply("🗑 Queue is empty — nothing to drop.")
        else:
            preview = "\n".join(f"  • {_oneline(t, 80)}" for t in dropped[:5])
            more = f"\n  …and {n - 5} more" if n > 5 else ""
            await reply(f"🗑 Dropped {n} queued message{'' if n == 1 else 's'}:\n{preview}{more}")
        return True

    # "bot issues" — dump recent tool errors (the ⚠️ N issue(s) detail) and clear them, like
    # draining an inbox. In-memory since the last bridge start; the turn summary shows only the
    # count to keep it clean.
    if re.match(r"^issues?\b", rest.strip(), re.IGNORECASE):
        n = len(_recent_issues)
        log.info("bot command: issues -> %d", n)
        if not n:
            await reply("✅ No issues — the inbox is clean.")
        else:
            shown = _recent_issues[-25:]
            lines = "\n".join(f"  {ts} · {txt}" for ts, txt in shown)
            older = f"\n(+{n - len(shown)} older, also cleared)" if n > len(shown) else ""
            _recent_issues[:] = []  # drain on read
            await reply(f"⚠️ {n} issue(s) — cleared:\n{lines}{older}")
        return True

    action = classify_bot_command(rest)
    log.info("bot command: %r -> %s", rest, action)

    if action == "new":
        await controller.reset()
        await reply("🆕 Fresh conversation (new session).")
    elif action == "interrupt":
        # Bare Esc/Ctrl-C: stop the current turn but keep the CLI connected — background shells
        # and session context survive (unlike stop(), which disconnects and drops bg work).
        cur = registry.current()
        interrupted = await cur.controller.interrupt_turn()
        ensure_worker(cur)           # mirror stop's dispatcher-revive safety
        cur.pending_event.set()
        badge = registry.badge(cur)
        if interrupted:
            await reply(badge + "⏸ Interrupted the current turn — background shells and the "
                        "session are untouched. Send your next message.")
        else:
            await reply(badge + "⏸ Nothing to interrupt — Claude is idle.")
    elif action == "stop":
        cur = registry.current()
        await cur.controller.stop()  # interrupt + clean reset (no post-interrupt wedge)
        ensure_worker(cur)           # revive the dispatcher if the interrupt killed it
        cur.pending_event.set()
        await reply("✋ Stopped — turn interrupted + connection reset (background work dropped); "
                    "session resumes on your next message.")
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
        # Send a raw /compact (no guard prefix) to the CURRENT session and stream the outcome.
        await dispatch_to_claude(
            context, registry.current(), chat_id, reply_to, "/compact", "command",
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


def prune_old_images() -> None:
    """Delete cached images older than IMAGE_MAX_AGE so a long no-restart session doesn't pile
    them up. Safe: a freshly-queued image is seconds old, never near the threshold."""
    try:
        cutoff = time.time() - IMAGE_MAX_AGE
        for f in IMAGE_TMP.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An image IS the input — no transcription (unlike audio). Download it and hand Claude
    the path (+ caption if any); Claude views it with the Read tool. Same state-machinery as
    the other handlers: auth → sleep → blocked → enqueue. Caption is the instruction when
    present; otherwise Claude infers the purpose from conversation context. We do NOT batch or
    wait for anything — you pace it; if the normal batcher happens to fuse it with an adjacent
    message, that's fine."""
    msg = update.message
    if not is_authorized(update):
        await handle_intrusion(update, context)
        return
    # Largest rendition of a sent photo, or an image sent as a file (document).
    media = None
    if msg.photo:
        media = msg.photo[-1]
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        media = msg.document
    if media is None:
        return
    # Sleep mode: ignore ALL Telegram input (wake only at the machine).
    if is_sleeping():
        log.info("Ignoring image — sleep mode engaged")
        await msg.reply_text(SLEEP_MSG)
        return
    caption = (msg.caption or "").strip()
    user = msg.from_user
    log.info("Image from %s (%s): file_id=%s caption=%r",
             user.full_name if user else "?", user.id if user else "?",
             media.file_id, caption)
    if is_blocked():
        await msg.reply_text(BLOCKED_MSG)
        return
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    # Download to a dedicated dir. UNLIKE audio we do NOT delete it here: Claude reads the
    # file during its (later) turn. Leftovers are swept at startup.
    IMAGE_TMP.mkdir(parents=True, exist_ok=True)
    prune_old_images()  # self-bound: drop cached images older than IMAGE_MAX_AGE
    ext = ".jpg"
    fname = getattr(media, "file_name", None)
    if fname and "." in fname:
        ext = "." + fname.rsplit(".", 1)[-1].lower()[:8]
    with tempfile.NamedTemporaryFile(suffix=ext, dir=IMAGE_TMP, delete=False) as tmp:
        path = tmp.name
    try:
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(path)
    except Exception:
        log.exception("Image download failed")
        await msg.reply_text("⚠️ Couldn't download that image.")
        return
    if caption:
        text = (f"[The user sent an image, saved at {path}. Caption: {caption}]\n"
                "View it with the Read tool, then respond.")
    else:
        text = (f"[The user sent an image, saved at {path}, with no caption.]\n"
                "View it with the Read tool and respond based on our conversation context.")
    target = registry.current()
    set_no_more_work(target, False)  # new work from the user re-arms the idle nudger
    target.parked = False            # ...and un-parks it
    enqueue_for_claude(target, msg.chat_id, msg.message_id, text, "image", False)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not is_authorized(update):
        await handle_intrusion(update, context)
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
    target = registry.current()
    set_no_more_work(target, False)  # new work from the user re-arms the idle nudger
    target.parked = False            # ...and un-parks it
    enqueue_for_claude(target, msg.chat_id, msg.message_id, text, "text", False)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await handle_intrusion(update, context)
        return
    await registry.current().controller.reset()
    await update.message.reply_text("🆕 Fresh conversation (new session).")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await handle_intrusion(update, context)
        return
    cur = registry.current()
    await cur.controller.stop()
    ensure_worker(cur)
    cur.pending_event.set()
    await update.message.reply_text("✋ Stopped — turn interrupted, session kept.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await handle_intrusion(update, context)
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


# The DRIVEN Claude may only reconfigure itself through these (the safe subset — the first
# word of the command). Everything else (new/clear/stop/kill/sleep/restart/lock/harness…) is
# refused, so a misheard request can't wipe, silence, or unlock the bridge. Security state
# (firewall/intrusion) isn't a bot command at all, so it's untouchable regardless.
SELFCONFIG_ALLOWED = {
    "effort", "model", "voice", "transcribe", "transcription", "quality", "tx",
    "cwd", "chdir", "workdir", "pwd", "status", "context", "ctx", "park",
}


async def _run_selfconfig(bot, chat, cmd: str) -> None:
    """Run ONE self-config command (from the driven Claude) through the ordinary
    bot-command handler, gated to the safe subset and attributed visibly to the bot."""
    verb = cmd.split(None, 1)[0].lower() if cmd else ""
    if verb not in SELFCONFIG_ALLOWED:
        log.warning("SELFCONFIG refused: %s", _oneline(cmd, 120))
        await bot.send_message(chat, f"🔧 self-config: refused `{cmd}` — not a settable config.")
        return
    log.info("SELFCONFIG <- claude: %s", _oneline(cmd, 200))
    await bot.send_message(chat, f"🔧 self-config (by the bot): `{cmd}`")
    try:
        await maybe_handle_bot_command(types.SimpleNamespace(bot=bot), chat, None, f"bot {cmd}")
    except Exception:
        log.exception("self-config command failed: %s", cmd)
        await bot.send_message(chat, "🔧 self-config: that command errored.")


async def cmd_inbox_loop(application) -> None:
    """Self-config channel — fully in-bridge, no harness involved. The DRIVEN Claude drops a
    command in cmd-inbox/ (via ./cg-cmd), and we run it through the SAME bot-command handler
    the owner uses, limited to the safe config subset. Atomic drop (.tmp then rename) so we
    never read a partial; consume-once (delete before running)."""
    try:
        CMD_INBOX.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("Could not create cmd inbox dir %s", CMD_INBOX)
        return
    while True:
        try:
            chat = sorted(ALLOWED_USER_IDS)[0] if ALLOWED_USER_IDS else None
            if chat is not None:
                for f in sorted(CMD_INBOX.iterdir()):
                    if (not f.is_file()) or f.suffix != ".cmd":
                        continue
                    try:
                        cmd = f.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        continue
                    try:
                        f.unlink()  # consume-once: delete before running
                    except OSError:
                        pass
                    if cmd:
                        await _run_selfconfig(application.bot, chat, cmd)
        except Exception:
            log.exception("cmd inbox loop error")
        await asyncio.sleep(1.0)


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


def _wake_echo_line(text: str) -> str:
    """One-line chat render of an incoming wake: a clock for the cron heartbeat, an inbox
    glyph for anything else (another bot, a program, a manual cg-wake). cg-wake is a plain
    program — the host can't know who called it, only what they wrote — so any sender
    identity lives inside `text`, shown verbatim."""
    icon = "⏰" if text.startswith("tick ") else "\U0001f4e8"  # cron clock / other
    return f"{icon} {text}"


def _drain_wake_inbox(chat) -> list[str]:
    """Process finished drops in WAKE_INBOX once: inject each as a turn into the CURRENT
    session (source 'wake'), consume-once (delete before enqueue). Returns the injected
    texts so the loop can echo each into the chat (so the owner sees WHAT woke the bot, not
    only its reply). Split from the loop so it is unit-testable without the infinite loop."""
    injected: list[str] = []
    try:
        entries = sorted(WAKE_INBOX.iterdir())
    except OSError:
        return injected
    for f in entries:
        if (not f.is_file()) or f.name.startswith(".") or f.suffix != ".msg":
            continue  # not a finished drop
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        try:
            f.unlink()  # consume-once: delete before enqueue, never double-inject
        except OSError:
            pass
        if text:
            enqueue_for_claude(registry.current(), chat, None, text, "wake", False)
            log.info("wake -> %s: %s", registry.current().name, _oneline(text, 120))
            injected.append(text)
    return injected


async def wake_inbox_loop(application) -> None:
    """External -> bot-turn injection. A scheduler (cron via ./cg-wake) or a peer program
    drops a message in wake-inbox/; we inject it as a turn into the current bot session.
    cron is the first client (a 3h heartbeat: "anything to do?"); the same drop shape will
    serve bot-to-bot messaging later. Atomic drop then rename; consume-once."""
    try:
        WAKE_INBOX.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("Could not create wake inbox dir %s", WAKE_INBOX)
        return
    while True:
        try:
            chat = sorted(ALLOWED_USER_IDS)[0] if ALLOWED_USER_IDS else None
            if chat is not None:
                echoed = False
                for text in _drain_wake_inbox(chat):
                    try:  # echo the incoming wake so the owner sees what woke the bot
                        await reply_chunked_bot(application.bot, chat, None, _wake_echo_line(text))
                        echoed = True
                    except Exception:
                        log.exception("wake echo failed")
                if echoed:
                    mark_sent()  # we posted to the chat; keep the watchdog quiet
        except Exception:
            log.exception("wake inbox loop error")
        await asyncio.sleep(1.0)


async def _drain_media_outbox(tgbot, chat) -> int:
    """One scan of media-outbox/: send every staged file to `chat`; returns how many
    were sent. Route by type UP FRONT — Telegram's sendPhoto silently accepts a PDF
    and rasterizes page 1, so a "try photo first" scheme mangles documents. A failed
    photo still falls back to document (the universal container: oversized/odd images
    arrive as files), but a failed document send is NEVER retried as a photo — a
    rasterized first page masquerading as the file is worse than a loud failure."""
    n = 0
    for f in sorted(MEDIA_OUTBOX.iterdir()):
        if (not f.is_file()) or f.name.startswith(".") or f.suffix in (".tmp", ".caption"):
            continue
        cap_file = f.with_suffix(".caption")
        caption = None
        if cap_file.exists():
            try:
                caption = cap_file.read_text(encoding="utf-8", errors="replace").strip() or None
            except OSError:
                caption = None
        sent = False
        is_image = f.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        try:
            with open(f, "rb") as fh:
                if is_image:
                    await tgbot.send_photo(chat, photo=fh, caption=caption)
                else:
                    await tgbot.send_document(chat, document=fh, caption=caption)
            sent = True
        except Exception:
            if is_image:
                log.warning("send_photo failed for %s — trying document", f.name, exc_info=True)
                try:
                    with open(f, "rb") as fh:
                        await tgbot.send_document(chat, document=fh, caption=caption)
                    sent = True
                except Exception:
                    log.exception("send_document also failed for %s", f.name)
            else:
                log.exception("send_document failed for %s", f.name)
        if sent:
            n += 1
            log.info("media sent: %s", f.name)
        for g in (f, cap_file):
            try:
                g.unlink()
            except OSError:
                pass
    return n


async def media_outbox_loop(application) -> None:
    try:
        MEDIA_OUTBOX.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("Could not create media outbox dir %s", MEDIA_OUTBOX)
        return
    while True:
        try:
            chat = sorted(ALLOWED_USER_IDS)[0] if ALLOWED_USER_IDS else None
            if chat is not None and await _drain_media_outbox(application.bot, chat):
                mark_sent()
        except Exception:
            log.exception("media outbox loop error")
        await asyncio.sleep(1.0)


async def on_startup(application) -> None:
    """Runs once the bot is initialized — log it and ping the owner(s) on Telegram
    that the bridge just came online (handy to see power-cycles from your phone)."""
    global _app
    _app = application
    # Load the canonical session BEFORE announcing, so the online banner and status text report
    # the configured bot (DEFAULT_BOT), not the vestigial 'claude' default that was current at
    # import. select_session wires the dispatcher (batches a burst into one turn), the spontaneous
    # relay (turns Claude starts on its own), and the silence-breaker watchdog. A dangling/unset
    # DEFAULT_BOT leaves the regenerated 'claude' default current. Extra sessions activate on
    # `bot select`. This must stay first: reorder it below the announce and the phone shows claude.
    canon = canonical_bot()
    if canon != DEFAULT_SESSION:
        select_session(canon)   # creates + sets current + controller + activates
    else:
        _activate_session(registry.current())
    log.info("bots discovered on disk: %s", ", ".join(discover_bots()) or "(none)")
    sid = controller.session_id
    log.info("🟢 claudegram online — session=%s cwd=%s", sid or "new", controller.get_cwd())
    text = "🟢 claudegram online\n" + await _status_text()
    for uid in sorted(ALLOWED_USER_IDS):
        try:
            await application.bot.send_message(uid, text)
        except Exception:
            log.exception("Could not send startup ping to %s", uid)
    mark_sent()  # the online ping counts — don't let the watchdog fire immediately
    if nostall_on():
        ensure_nostall_bot()  # anti-stall guard was left on — bring its bot up at startup
    _spawn(worker_guard(), name="worker_guard")
    log.info("default session activated (worker + relay + watchdog); guard started "
             "(debounce %.1fs)", BATCH_DEBOUNCE)
    # Start the IPC inbox so other programs on this machine can ping the phone.
    _spawn(harness_outbox_loop(application), name="harness_outbox")
    log.info("HARNESS outbox watcher started at %s", HARNESS_OUTBOX)
    _spawn(media_outbox_loop(application), name="media_outbox")
    log.info("media outbox watcher started at %s", MEDIA_OUTBOX)
    _spawn(cmd_inbox_loop(application), name="cmd_inbox")
    log.info("self-config command inbox started at %s", CMD_INBOX)
    _spawn(wake_inbox_loop(application), name="wake_inbox")
    log.info("wake inbox watcher started at %s", WAKE_INBOX)
    # Scrape subscription 5h/week usage from `claude /usage` every 10 min → cache → DONE line.
    _spawn(usage_collector_loop(), name="usage_collector")
    log.info("usage collector started (refresh %ss via usage_worker.py)", USAGE_REFRESH_SECS)


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
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo)
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
