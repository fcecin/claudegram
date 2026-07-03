import asyncio
import types

import bot
from tests.fakes import FakeBot, make_fake_session


def test_guard_bot_is_haiku_low_internal_no_voice_with_config_icon():
    name = bot.NOSTALL_BOT
    cfg = bot.bot_config(name)
    assert cfg["model"] == "haiku"
    assert cfg["effort"] == "low"
    assert cfg["voiceback"] is False
    assert cfg["internal"] is True
    assert cfg["icon"] == "🪓"          # the icon lives in config, not in bot.py
    s = bot.Session(name)
    assert s.internal is True
    assert s.emoji == "🪓"              # Session badge comes from its config icon
    assert s.controller.forced_model == "haiku"
    assert s.controller.effort == "low"


def test_bot_py_does_not_hardcode_the_icon():
    # The only jack-ish literal allowed in bot.py is the NAME. The icon must not appear in code.
    import pathlib
    src = pathlib.Path(bot.__file__).read_text()
    assert "🪓" not in src


def test_boot_scan_discovers_bots_including_the_guard():
    found = bot.discover_bots()
    assert bot.NOSTALL_BOT in found
    # a couple of the normal selectable bots are on disk too
    assert "sno" in found and "gol" in found
    assert bot.nostall_bot_available() is True


def test_guard_charter_main_md_holds_the_protocol():
    home = bot.bot_home(bot.NOSTALL_BOT)
    assert home is not None
    body = (home / "main.md").read_text()
    assert bot.NOSTALL_LEGIT_MARKER in body        # the output contract lives in main.md
    assert "var/" in body                           # the knowledge-base machinery is in main.md


def test_nostall_flag_toggles_and_is_off_by_default():
    if bot.NOSTALL_FILE.exists():
        bot.NOSTALL_FILE.unlink()
    assert bot.nostall_on() is False
    bot.set_nostall(True)
    assert bot.nostall_on() is True
    bot.set_nostall(False)
    assert bot.nostall_on() is False


def test_ensure_guard_only_when_on_and_does_not_multiplex():
    registry = bot.registry
    registry.sessions.pop(bot.NOSTALL_BOT, None)
    bot.set_nostall(False)
    assert bot.ensure_nostall_bot() is None            # guard off => nothing
    assert bot.NOSTALL_BOT not in registry.sessions
    bot.set_nostall(True)
    g = bot.ensure_nostall_bot()
    assert g is not None and g.name == bot.NOSTALL_BOT
    assert bot.NOSTALL_BOT in registry.sessions
    # internal: it must NOT flip a solo install into multiplexing / color-tag mode.
    assert registry.multiplexing() is False
    assert bot.registry.badge(registry.current()) == ""
    assert bot.ensure_nostall_bot() is g              # idempotent
    registry.sessions.pop(bot.NOSTALL_BOT, None)
    bot.set_nostall(False)


def test_guard_excluded_from_sessions_overview():
    bot.set_nostall(True)
    bot.ensure_nostall_bot()
    overview = bot._sessions_overview()
    assert bot.NOSTALL_BOT not in overview
    bot.registry.sessions.pop(bot.NOSTALL_BOT, None)
    bot.set_nostall(False)


async def test_bot_nostall_command_on_off():
    fb = FakeBot()
    if bot.NOSTALL_FILE.exists():
        bot.NOSTALL_FILE.unlink()
    ctx = types.SimpleNamespace(bot=fb)
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot nostall on")
    assert bot.nostall_on() is True
    assert any("Anti-stalling guard is ON" in s for s in fb.sent), fb.sent
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot nostall off")
    assert bot.nostall_on() is False
    assert any("Anti-stalling guard is OFF" in s for s in fb.sent), fb.sent


async def test_nostall_cannot_turn_on_without_its_bot(monkeypatch=None):
    # If the guard's bot isn't installed, the guard refuses to turn on.
    fb = FakeBot()
    orig = bot.discover_bots
    bot.discover_bots = lambda: {"sno": {}, "gol": {}}   # roster without the guard bot
    try:
        ctx = types.SimpleNamespace(bot=fb)
        await bot.maybe_handle_bot_command(ctx, 1, None, "bot nostall on")
        assert bot.nostall_on() is False
        assert any("isn't installed" in s for s in fb.sent), fb.sent
    finally:
        bot.discover_bots = orig


def test_nostall_cleared_flag_defaults_off_and_charter_allows_a_reason():
    assert bot.Session("claude").nostall_cleared is False
    # the guard may append a one-line reason after LEGIT STOP (shown to the human)
    body = (bot.bot_home(bot.NOSTALL_BOT) / "main.md").read_text()
    assert "reason" in body.lower()


def test_charter_forbids_pushing_the_bot_to_do_less():
    # The guard has no scope/context, so it must only ever demand MORE work — never tell a
    # bot to stop, narrow, or that something is "out of scope" (it can't know that).
    body = (bot.bot_home(bot.NOSTALL_BOT) / "main.md").read_text().lower()
    assert "out of scope" in body
    assert "never less" in body or "only ever push for more" in body


def test_charter_affirms_the_bots_own_next_step_instead_of_curtailing():
    # The guard only ever fires AFTER a bot has stopped, so "leave it working" is not a move;
    # when the stopped bot's parting words name more work, the counter is to affirm+continue,
    # not "out of scope, stop".
    body = " ".join((bot.bot_home(bot.NOSTALL_BOT) / "main.md").read_text().lower().split())
    assert "already stopped" in body            # it never catches a bot mid-work
    assert "continue, do not stop" in body      # the affirm-and-drive move


def test_recent_answers_buffer_is_bounded():
    s = bot.Session("claude")
    for i in range(bot.NOSTALL_FEED_MSGS + 5):
        s.recent_answers.append(f"answer {i}")
    assert len(s.recent_answers) == bot.NOSTALL_FEED_MSGS
    assert s.recent_answers[-1] == f"answer {bot.NOSTALL_FEED_MSGS + 4}"


async def test_police_runs_off_the_watchdog_critical_path():
    # THE REGRESSION: a slow guard turn (jack took ~83s once) froze the watched bot's watchdog
    # because _police_stall was awaited INLINE in the loop. Now it's spawned off the critical
    # path: _spawn_police fires the consult as a task and returns immediately, and a second call
    # while one is in flight does not stack.
    wd = bot.Watchdog.__new__(bot.Watchdog)
    wd._police_task = None
    gate = asyncio.Event()
    started = asyncio.Event()
    calls = []

    async def _fake_police(reason):
        calls.append(reason)
        started.set()
        await gate.wait()               # simulate a slow / hung guard turn
        return True

    wd._police_stall = _fake_police

    wd._spawn_police("first")
    t1 = wd._police_task
    assert t1 is not None
    await started.wait()                # consult began — deterministic, no sleep race
    assert not t1.done()                # still blocked, yet _spawn_police already returned
    assert calls == ["first"]

    wd._spawn_police("second")          # one already in flight -> no stacking
    assert wd._police_task is t1
    assert calls == ["first"]

    gate.set()
    await t1
    assert t1.done()

    started.clear()
    wd._spawn_police("third")           # the in-flight one finished -> a new consult is allowed
    await wd._police_task
    assert calls == ["first", "third"]


async def test_police_posts_a_reviewing_one_liner_to_the_owner():
    # When the guard actually consults, the OWNER gets a 'reviewing…' one-liner up front so the
    # (possibly slow) reasoning window reads as activity, not a frozen watchdog. Owner only — it's
    # a send_message, never injected into the reviewed bot.
    fb = FakeBot()
    sess = make_fake_session("claude")
    sess.recent_answers.append("I'll stop here; nothing left to do.")
    guard = make_fake_session(bot.NOSTALL_BOT)
    wd = bot.Watchdog.__new__(bot.Watchdog)
    wd.app = types.SimpleNamespace(bot=fb)
    wd.session = sess
    wd._nostall_last = 0.0
    wd._chat = lambda: 12345

    orig_ensure, orig_ask = bot.ensure_nostall_bot, bot.ask_text

    async def _fake_ask(g, p):
        return "LEGIT STOP — done and green"

    bot.ensure_nostall_bot = lambda: guard
    bot.ask_text = _fake_ask
    try:
        result = await wd._police_stall("it's idle with nothing running")
    finally:
        bot.ensure_nostall_bot, bot.ask_text = orig_ensure, orig_ask

    assert result is True
    assert any("reviewing" in s for s in fb.sent), fb.sent           # the up-front one-liner
    assert any("genuinely done" in s for s in fb.sent), fb.sent      # the verdict lands below it
