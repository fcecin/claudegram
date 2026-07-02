"""Test doubles for claudegram — the whole point: the bridge has exactly TWO external
edges, so faking both makes ~all of the logic testable offline and deterministically:

  1. Telegram   -> FakeBot / FakeApp   (record send/edit/delete, no network, no token)
  2. Claude     -> FakeController      (ask() drives a SCRIPTED sequence of SDK messages
                                        into the renderer, no model, no subscription)

Plus SDK-message factories so a fake "Claude turn" is a few lines, and a stub transcribe
worker (a fake whisper) so handle_audio is testable with no model/GPU.

No pytest: import these from tests/test_*.py and assert. See tests/run.py for the runner."""

import itertools
import logging

import bot
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, StreamEvent, SystemMessage,
    TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage,
)

# Quiet the bridge's INFO chatter during tests (turn logs, worker (re)start, etc.).
logging.getLogger("claudegram").setLevel(logging.ERROR)

# Make debounced paths fast so tests don't sleep for real seconds.
bot.BATCH_DEBOUNCE = 0.02
bot.ParagraphStreamer.COALESCE_SECS = 0.05

_uid = itertools.count(1)


# --- Telegram side --------------------------------------------------------------
class FakeBot:
    """Records every Telegram call. `sent` is the ordered list of message texts."""

    def __init__(self):
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.deleted: list = []
        self.voices: int = 0
        self.photos: list = []
        self.documents: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return _Msg(len(self.sent))

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)

    async def delete_message(self, chat_id, message_id, **kw):
        self.deleted.append(message_id)

    async def send_voice(self, chat_id, voice=None, **kw):
        self.voices += 1

    async def send_photo(self, chat_id, photo=None, caption=None, **kw):
        self.photos.append(caption)

    async def send_document(self, chat_id, document=None, caption=None, **kw):
        self.documents.append(caption)

    async def send_chat_action(self, chat_id, action, **kw):
        pass


class _Msg:
    def __init__(self, mid):
        self.message_id = mid


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()


# --- Claude side ----------------------------------------------------------------
class FakeController:
    """A ClaudeController stand-in. ask() feeds a scripted list of SDK messages into the
    renderer's on_event — so you can make "Claude" do ANYTHING (stream text, call a tool,
    lead with the firewall sentinel, hit a rate limit, end with NO MORE WORK) with zero
    model calls. Set `script` (list of messages) per turn, or `scripts` (list of lists)
    to serve successive turns."""

    def __init__(self, session_id="fakesess", model="fake-model", script=None, scripts=None,
                 raises=None):
        self.session_id = session_id
        self.model = model
        self.busy = False
        self.raises = raises
        self.asked: list[str] = []
        self._spontaneous = None
        self._scripts = list(scripts) if scripts is not None else None
        self._script = list(script) if script is not None else []

    def set_spontaneous_handler(self, handler):
        self._spontaneous = handler

    async def ask(self, prompt, on_event, on_system=None):
        self.asked.append(prompt)
        if self.raises is not None:
            raise self.raises
        script = self._scripts.pop(0) if self._scripts else self._script
        self.busy = True
        try:
            for m in script:
                await on_event(m)
        finally:
            self.busy = False

    async def push_spontaneous(self, messages):
        """Simulate a self-started turn (a background shell landed) by driving messages
        into the registered spontaneous handler — the behavior the monitor model rests on."""
        for m in messages:
            await self._spontaneous(m)

    async def context_usage(self):
        return {"percentage": 42.0}

    async def interrupt(self):
        pass

    async def kill(self):
        return []

    async def stop(self):
        pass

    async def reset(self):
        self.session_id = None

    def status(self):
        return {"active": self.busy, "segment_secs": 0, "shells": [],
                "idle_secs": 0, "connected": True}

    def get_effort(self):
        return "low"

    def get_cwd(self):
        return "/tmp/fake"


# --- SDK message factories (a scripted "Claude turn") ---------------------------
def sys_init(session_id="fakesess", model="fake-model"):
    return SystemMessage(subtype="init", data={"session_id": session_id, "model": model})


def stream_text(delta, session_id="fakesess"):
    return StreamEvent(uuid=str(next(_uid)), session_id=session_id,
                       event={"type": "content_block_delta",
                              "delta": {"type": "text_delta", "text": delta}})


def stream_thinking_start(session_id="fakesess"):
    return StreamEvent(uuid=str(next(_uid)), session_id=session_id,
                       event={"type": "content_block_start",
                              "content_block": {"type": "thinking"}})


def assistant_text(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="fake-model")


def assistant_tool(name, inp, tool_id=None):
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_id or f"t{next(_uid)}", name=name, input=inp)],
        model="fake-model")


def assistant_thinking(text):
    return AssistantMessage(content=[ThinkingBlock(thinking=text, signature="")],
                            model="fake-model")


def tool_result(tool_use_id, content, is_error=False):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_use_id, content=content,
                                                is_error=is_error)])


def result_msg(is_error=False, subtype="success", result="", num_turns=1,
               duration_ms=1000, session_id="fakesess"):
    return ResultMessage(subtype=subtype, duration_ms=duration_ms, duration_api_ms=duration_ms,
                         is_error=is_error, num_turns=num_turns, session_id=session_id,
                         result=result)


# --- session helpers ------------------------------------------------------------
def make_fake_session(name="claude", script=None, scripts=None, session_id="fakesess"):
    """A Session whose controller is a FakeController (real ClaudeController discarded)."""
    s = bot.Session(name)
    s.controller = FakeController(session_id=session_id, script=script, scripts=scripts)
    return s


def reset_registry():
    """Restore the registry to a single default 'claude' session (cancel any spawned
    worker/watchdog tasks first). Call between tests that touch the registry."""
    for s in list(bot.registry.sessions.values()):
        for t in (s.worker_task, s.watchdog_task):
            if t is not None and not t.done():
                t.cancel()
    bot.registry.sessions.clear()
    bot.registry.current_name = bot.DEFAULT_SESSION
    bot.registry.ensure_default()
    bot.controller = bot.registry.current().controller


def clear_flags():
    """Remove any lock/sleep flags a test may have written (worktree-local, gitignored)."""
    for f in (bot.BLOCK_FILE, bot.SLEEP_FILE):
        try:
            f.unlink()
        except OSError:
            pass
