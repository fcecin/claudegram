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
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from faster_whisper import WhisperModel
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


# --- Whisper model (loaded once, transcription runs in a thread pool) ----------

# Defaults tuned for maximum accuracy: the largest model at full precision.
# This is CPU-heavy but correctness matters more than latency here.
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "large-v3").strip()
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu").strip()
COMPUTE_TYPE = os.environ.get(
    "WHISPER_COMPUTE_TYPE", "float32" if DEVICE == "cpu" else "float16"
).strip()
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "").strip() or None


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


_model: WhisperModel | None = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        log.info(
            "Loading whisper model '%s' (device=%s, compute=%s) — "
            "first run downloads it, please wait...",
            MODEL_SIZE,
            DEVICE,
            COMPUTE_TYPE,
        )
        _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        log.info("Model loaded.")
    return _model


def transcribe(path: str) -> dict:
    """Blocking transcription. Returns text + diagnostics for quality testing."""
    model = get_model()
    t0 = time.monotonic()
    segments, info = model.transcribe(path, language=LANGUAGE, beam_size=5, vad_filter=True)
    # segments is a generator; consuming it is where the work actually happens.
    text = "".join(seg.text for seg in segments).strip()
    return {
        "text": text,
        "language": info.language,
        "language_probability": info.language_probability,
        "audio_seconds": info.duration,
        "elapsed_seconds": time.monotonic() - t0,
    }


# --- Claude Code control ------------------------------------------------------

CGHOME = Path(os.environ.get("CGHOME", str(Path.home() / "cghome"))).expanduser()
SESSION_FILE = HERE / "session.id"   # persisted Claude session id (for resume)
EFFORT_FILE = HERE / "effort.level"  # persisted reasoning effort
CWD_FILE = HERE / "cwd.path"         # persisted working directory
LOG_PATH = HERE / "claudegram.log"   # bridge log (written by the tray supervisor)
AUDIO_TMP = Path(tempfile.gettempdir()) / "claudegram_audio"  # transient voice files
controller = ClaudeController(
    str(CGHOME), str(SESSION_FILE), str(EFFORT_FILE), str(CWD_FILE)
)


def ensure_cghome() -> None:
    CGHOME.mkdir(parents=True, exist_ok=True)


def sweep_audio_tmp() -> None:
    """Clear leftover voice temp files (e.g. from a crash mid-transcription)."""
    try:
        AUDIO_TMP.mkdir(parents=True, exist_ok=True)
        for f in AUDIO_TMP.iterdir():
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


def build_prompt(user_text: str, source: str) -> str:
    # Lean prepend: short guard only. Regressions live in a file the model reads
    # when unsure (referenced in the guard) — not injected, to avoid context bloat.
    guard = GUARD_AUDIO if source == "audio" else GUARD_TEXT
    return f"{guard}\n{user_text}"


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
        # Transcription is CPU-bound and blocking — keep the event loop free.
        try:
            result = await asyncio.to_thread(transcribe, tmp_path)
        except Exception:
            log.exception("Transcription failed")
            await msg.reply_text("⚠️ Sorry, I couldn't transcribe that.")
            return
    finally:
        try:
            os.remove(tmp_path)  # delete the audio as soon as transcription ends
        except OSError:
            pass

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
    await dispatch_to_claude(context, msg.chat_id, msg.message_id, text, "audio")


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
    """One Telegram message, edited in place to show a live activity feed."""

    def __init__(self, bot, chat_id: int, reply_to, header: str):
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.header = header
        self.lines: list[str] = []
        self.message_id = None
        self._last_edit = 0.0
        self._min_interval = 1.2  # throttle edits (Telegram rate limits)

    def _render(self) -> str:
        body = "\n".join(self.lines[-22:])
        text = self.header + (("\n\n" + body) if body else "")
        return text[-3900:]

    async def start(self) -> None:
        m = await self.bot.send_message(
            self.chat_id, self._render(), reply_to_message_id=self.reply_to
        )
        self.message_id = m.message_id

    async def add(self, line: str) -> None:
        self.lines.append(line)
        await self._flush(force=False)

    async def _flush(self, force: bool) -> None:
        if self.message_id is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < self._min_interval:
            return
        self._last_edit = now
        try:
            await self.bot.edit_message_text(
                self._render(), chat_id=self.chat_id, message_id=self.message_id
            )
        except Exception:
            pass  # "message is not modified" / transient — ignore

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
            await self.bot.send_message(
                self.chat_id, text[i:i + self.TG_LIMIT],
                reply_to_message_id=self.reply_to if not self.sent_any else None,
            )
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
        await self.bot.send_message(self.chat_id, "[[END]]")


async def dispatch_to_claude(
    context, chat_id, reply_to, user_text: str, source: str,
    raw: bool = False, header: str = "🤖 Claude is working…",
) -> None:
    """Stream one turn to the chat. Normally the prompt is guarded (firewall); with
    raw=True it is sent verbatim (used for slash commands like /compact)."""
    bot = context.bot
    if controller.busy:
        await bot.send_message(chat_id, "⏳ Still on the previous request — queuing this one.")

    board = StatusBoard(bot, chat_id, reply_to, header)
    await board.start()

    state = {"thinking": False, "tripped": False, "result": None, "tools": {}}
    answer_buf: list[str] = []  # accumulate streamed answer text for logging

    # The answer text streams out as paragraph messages (a block reply streams too,
    # so you can see the model's stated reason).
    streamer = ParagraphStreamer(bot, chat_id, reply_to)

    async def trip(model_reason: str = "") -> None:
        if state["tripped"]:
            return
        state["tripped"] = True
        await streamer.flush()  # flush whatever already streamed (no [[END]])
        engage_block(_oneline(user_text, 1000))
        log.warning("🔒 BLOCKED prompt: %s", user_text)
        if model_reason:
            log.warning("Block reasoning: %s", model_reason)
        try:
            await controller.interrupt()
        except Exception:
            pass
        await board.finish("🛑 HACKING ATTEMPT BLOCKED — bridge locked")
        msg = BLOCKED_MSG
        if model_reason:
            msg += f"\n\nClaude's reason: {_oneline(model_reason, 400)}"
        await bot.send_message(chat_id, msg)

    async def on_system(kind: str, data: dict) -> None:
        if kind == "compaction_started":
            log.info("🗜 Auto-compaction started (%s)", data.get("trigger", "auto"))
            await board.add(
                f"🗜 Auto-compaction started ({data.get('trigger', 'auto')}) — "
                "summarizing the conversation to free up context…"
            )

    async def on_event(message) -> None:
        if state["tripped"]:
            return
        # Live text deltas -> stream into paragraph messages.
        if isinstance(message, StreamEvent):
            ev = message.event
            t = ev.get("type")
            if t == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    state["thinking"] = False
                    chunk = delta.get("text", "")
                    answer_buf.append(chunk)
                    await streamer.feed(chunk)
            elif t == "content_block_start":
                if ev.get("content_block", {}).get("type") == "thinking" and not state["thinking"]:
                    await board.add("💭 thinking…")
                    state["thinking"] = True
            return
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingBlock):
                    log.info("THINKING: %s", block.thinking)
                    if not state["thinking"]:
                        await board.add("💭 thinking…")
                        state["thinking"] = True
                elif isinstance(block, ToolUseBlock):
                    state["thinking"] = False
                    state["tools"][block.id] = block.name
                    log.info("TOOL %s input=%r", block.name, block.input)
                    await board.add(summarize_tool(block.name, block.input or {}))
                elif isinstance(block, TextBlock):
                    # already streamed via StreamEvent — detect a block (it leads
                    # with the sentinel) and capture the model's stated reason.
                    if sentinel_tripped(block.text):
                        reason = "\n".join(block.text.splitlines()[1:]).strip()
                        await trip(reason)
                        return
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    name = state["tools"].get(block.tool_use_id, "")
                    log.info("RESULT %s err=%s: %s", name or "?",
                             block.is_error, _blocktext(block.content))
                    line = summarize_result(name, block.content, block.is_error)
                    if line:
                        await board.add(line)
        elif isinstance(message, SystemMessage):
            log.info("SYSTEM subtype=%s", message.subtype)
            if "compact" in (message.subtype or "").lower():
                await board.add("🗜 Compaction finished — context summarized.")
        elif isinstance(message, ResultMessage):
            state["result"] = {
                "turns": message.num_turns,
                "secs": (message.duration_ms or 0) / 1000,
                "is_error": bool(message.is_error)
                or (message.subtype not in (None, "success")),
                "subtype": message.subtype,
                "text": (message.result or "").strip(),
            }

    prompt = user_text if raw else build_prompt(user_text, source)
    try:
        await controller.ask(prompt, on_event, on_system=on_system)
    except Exception:
        log.exception("Claude turn failed")
        if not state["tripped"]:
            await board.finish("⚠️ Something went wrong driving Claude.")
        return

    if state["tripped"]:
        return

    res = state["result"] or {}
    final = res.get("text", "")
    # Backstop firewall check on the full answer (in case it never streamed).
    if final and sentinel_tripped(final):
        await trip("\n".join(final.splitlines()[1:]).strip())
        return
    # If nothing streamed (answer arrived only in the result), send it now.
    if not streamer.sent_any and final:
        await streamer.feed(final)
    await streamer.finish()  # flush remainder + [[END]]

    answer = "".join(answer_buf).strip() or final
    log.info("ANSWER (%d chars):\n%s", len(answer), answer)

    ctx = await controller.context_usage()
    ctx_str = f" · ctx {ctx['percentage']:.0f}%" if ctx else ""
    turns, secs = res.get("turns", "?"), res.get("secs", 0)
    if res.get("is_error"):
        await board.finish(f"⚠️ Ended: {res.get('subtype')} · {turns} turns · {secs:.0f}s{ctx_str}")
    else:
        await board.finish(f"✅ Done · {turns} turns · {secs:.0f}s{ctx_str}")
    log.info("TURN DONE: subtype=%s turns=%s secs=%.1f%s session=%s",
             res.get("subtype"), turns, secs, ctx_str, controller.session_id)


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
    "• bot effort [level] — show/set reasoning effort (low|medium|high|xhigh|max)\n"
    "• bot cwd [path] — show/set Claude's working directory\n"
    "• bot context — detailed context-window usage\n"
    "• bot logs [n] — last n bridge log lines\n"
    "• bot restart — restart the bridge process\n"
    "• bot echo <text> — echo text back (not sent to Claude)\n"
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
    ctx = await controller.context_usage()
    ctx_str = f" · ctx {ctx['percentage']:.0f}%" if ctx else ""
    return (
        f"✅ Bridge OK · Claude {busy} · effort {eff} · "
        f"session {sid_str}{ctx_str}{blocked} · cwd {controller.get_cwd()}"
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
            await reply(f"📂 Working dir set to: {controller.get_cwd()} (fresh session started there).")
        else:
            await reply(f"📂 Couldn't switch to: {target}")
        return True

    # "bot logs [n]" — last N lines of the bridge log.
    m = re.match(r"^logs?\b\s*(\d*)$", rest.strip(), re.IGNORECASE)
    if m:
        n = max(1, min(int(m.group(1)), 60)) if m.group(1) else 20
        await reply("📜 last log lines:\n" + _tail_log(n))
        return True

    action = classify_bot_command(rest)
    log.info("bot command: %r -> %s", rest, action)

    if action == "new":
        await controller.reset()
        await reply("🆕 Fresh conversation (new session).")
    elif action == "stop":
        await controller.interrupt()
        await reply("✋ Interrupting the current task.")
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
        asyncio.create_task(_restart_bridge())
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
    # "bot ..." messages are harness commands and never reach Claude.
    if await maybe_handle_bot_command(context, msg.chat_id, msg.message_id, text):
        return
    if is_blocked():
        await msg.reply_text(BLOCKED_MSG)
        return
    await dispatch_to_claude(context, msg.chat_id, msg.message_id, text, "text")


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
    await controller.interrupt()
    await update.message.reply_text("✋ Interrupting the current task.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("🚫 This is a private bot.")
        return
    await update.message.reply_text(await _status_text())


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)


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

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)  # so /stop runs while a turn is in progress
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

    # Warm up the transcription model before polling so the first voice is fast.
    get_model()

    log.info("claudegram bridge is up. Talk to your bot (voice or text).")
    # drop_pending_updates=False: messages sent while we were offline are held by
    # Telegram (for ~24h) and delivered when we reconnect, so nothing is skipped.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
