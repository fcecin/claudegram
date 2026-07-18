"""`bot interrupt` / `bot int` — the bare Esc/Ctrl-C: stop the current turn but keep the CLI
connected so background shells and the session context SURVIVE. This is the lightest of the
three teardowns (interrupt_turn < stop < kill). The live experiment proved a bare interrupt
leaves `run_in_background` shells running; these pin the controller's turn-release logic and
the command wiring so it stays that way.
"""
import asyncio
import types

import bot
import claude_driver
from tests.fakes import FakeBot, FakeController, make_fake_session, result_msg


def _controller(in_segment=True, client=None, shells=None):
    # Build without __init__ (file/session I/O); set only what interrupt_turn touches.
    c = claude_driver.ClaudeController.__new__(claude_driver.ClaudeController)
    c._client = client
    c._child_pid = 123
    c.in_segment = in_segment
    c._cur_is_user = in_segment
    c._awaiting_user_segment = False
    c._segment_done = asyncio.Event()
    c.shells = shells if shells is not None else {}
    return c


class _CleanClient:
    """A CLI that, on interrupt(), cleanly ends the turn the way the reader would on the
    interrupt's ResultMessage: in_segment -> False and _segment_done set."""
    def __init__(self, c):
        self.c = c
        self.interrupted = False

    async def interrupt(self):
        self.interrupted = True
        self.c.in_segment = False
        self.c._cur_is_user = False
        self.c._segment_done.set()


class _DeadClient:
    """A CLI that accepts interrupt() but never closes the turn (the historical wedge)."""
    def __init__(self):
        self.interrupted = False

    async def interrupt(self):
        self.interrupted = True


class _RaisingClient:
    async def interrupt(self):
        raise RuntimeError("interrupt blew up")


# --- controller primitive -------------------------------------------------------

async def test_clean_interrupt_releases_ask_and_keeps_client_and_bg():
    c = _controller(shells={"t1": {"desc": "build"}})
    client = _CleanClient(c)
    c._client = client
    ok = await c.interrupt_turn(settle=1.0)
    assert ok is True
    assert client.interrupted is True
    assert c._segment_done.is_set()          # the waiting ask() is released
    assert c._client is client               # CLI kept connected (NOT dropped) -> session survives
    assert c.shells == {"t1": {"desc": "build"}}  # background shells untouched -> they keep running


async def test_fallback_releases_when_cli_never_ends_the_turn():
    c = _controller()
    client = _DeadClient()
    c._client = client
    ok = await c.interrupt_turn(settle=0.05)  # CLI never closes the turn -> local release
    assert ok is True
    assert client.interrupted is True
    assert c._segment_done.is_set()           # released ourselves, so the dispatcher can't wedge
    assert c.in_segment is False              # segment bookkeeping reset...
    assert c._cur_is_user is False
    assert c._awaiting_user_segment is False
    assert c._client is client                # ...but the client is STILL connected (bg preserved)


async def test_interrupt_survives_a_raising_interrupt():
    c = _controller()
    c._client = _RaisingClient()
    ok = await c.interrupt_turn(settle=0.05)  # interrupt() raises -> caught -> fallback release
    assert ok is True
    assert c._segment_done.is_set()
    assert c._client is not None              # still not dropped


async def test_noop_when_idle():
    c = _controller(in_segment=False)
    client = _CleanClient(c)
    c._client = client
    ok = await c.interrupt_turn(settle=0.05)
    assert ok is False                        # nothing running
    assert client.interrupted is False        # interrupt() never called


async def test_noop_when_disconnected():
    c = _controller(in_segment=True, client=None)
    assert await c.interrupt_turn(settle=0.05) is False


async def test_preserves_shells_on_both_paths():
    # Neither the clean nor the fallback path may clear background shells (that's what stop()/
    # kill() do; the whole point of interrupt_turn is NOT to).
    for make_client in (lambda c: _CleanClient(c), lambda c: _DeadClient()):
        c = _controller(shells={"b": {"desc": "server"}})
        c._client = make_client(c)
        await c.interrupt_turn(settle=0.05)
        assert c.shells == {"b": {"desc": "server"}}


# --- command wiring -------------------------------------------------------------

def test_classify_interrupt_is_distinct_from_stop_and_kill():
    assert bot.classify_bot_command("interrupt") == "interrupt"
    assert bot.classify_bot_command("int") == "interrupt"
    assert bot.classify_bot_command("stop") == "stop"
    assert bot.classify_bot_command("cancel") == "stop"       # stays a stop
    assert bot.classify_bot_command("kill") == "kill"


async def test_bot_interrupt_command_calls_interrupt_turn_and_replies():
    fb = FakeBot()
    cur = bot.registry.current()
    cur.controller = FakeController()
    cur.controller.busy = True                                # a turn is running
    ctx = types.SimpleNamespace(bot=fb)
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot interrupt")
    assert cur.controller.busy is False                       # turn ended
    assert any("Interrupted" in s for s in fb.sent), fb.sent


async def test_bot_int_alias_and_idle_reply():
    fb = FakeBot()
    cur = bot.registry.current()
    cur.controller = FakeController()
    cur.controller.busy = False                               # idle -> nothing to interrupt
    ctx = types.SimpleNamespace(bot=fb)
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot int")
    assert any("Nothing to interrupt" in s for s in fb.sent), fb.sent


async def test_interrupted_turn_closes_cleanly_not_as_a_crash():
    # The REAL interrupt ResultMessage is is_error=True (subtype error_during_execution) — the
    # isolated contract test confirmed it. With the interrupt flag set, dispatch must close
    # cleanly ([[END]] + "interrupted"), NOT post a crash. (Contrast:
    # test_turn.test_error_result_reports_crash_not_answer — same result, no flag -> a crash.)
    fb = FakeBot()
    sess = make_fake_session("claude", script=[
        result_msg(is_error=True, subtype="error_during_execution", result="")])
    sess.controller._interrupted = True                       # as if interrupt_turn() fired mid-turn
    await bot.dispatch_to_claude(types.SimpleNamespace(bot=fb), sess, 1, None, "hi", "text")
    allmsgs = fb.sent + fb.edited
    assert not any("crash" in s.lower() for s in allmsgs), allmsgs    # NOT reported as a crash
    assert "[[END]]" in fb.sent                                        # prompt freed
    assert any("interrupted" in s.lower() for s in allmsgs), allmsgs   # closed as interrupted


async def test_interrupts_pending_turn_before_init():
    # The blind window: query() sent, but the CLI hasn't emitted the init message yet —
    # in_segment is still False while a user turn IS in flight (_awaiting_user_segment).
    # The old in_segment-only check answered "Nothing to interrupt" here.
    c = _controller(in_segment=False)
    c._awaiting_user_segment = True
    client = _CleanClient(c)
    c._client = client
    ok = await c.interrupt_turn(settle=0.5)
    assert ok is True                         # the pending turn IS interruptible
    assert client.interrupted is True         # interrupt() actually reached the CLI
