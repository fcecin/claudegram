import types

import bot
from tests.fakes import FakeBot, make_fake_session


def test_boink_flag_toggles_and_is_off_by_default():
    if bot.BOINK_FILE.exists():
        bot.BOINK_FILE.unlink()
    assert bot.boink_on() is False
    bot.set_boink(True)
    assert bot.boink_on() is True
    bot.set_boink(False)
    assert bot.boink_on() is False


def test_boink_is_a_settable_selfconfig():
    # The driven bot may toggle its own backstop via cg-cmd (self-config subset).
    assert "boink" in bot.SELFCONFIG_ALLOWED


async def test_bot_boink_command_on_off():
    fb = FakeBot()
    if bot.BOINK_FILE.exists():
        bot.BOINK_FILE.unlink()
    ctx = types.SimpleNamespace(bot=fb)
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot boink on")
    assert bot.boink_on() is True
    assert any("BOINK is ON" in s for s in fb.sent), fb.sent
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot boink off")
    assert bot.boink_on() is False
    assert any("BOINK is OFF" in s for s in fb.sent), fb.sent


async def test_bare_bot_boink_queries_and_never_toggles():
    # A bare `bot boink` (no on/off) must QUERY, not flip — repeated sends were the bug that
    # made the old nostall toggle flap; boink must not repeat it.
    fb = FakeBot()
    bot.set_boink(True)
    ctx = types.SimpleNamespace(bot=fb)
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot boink")
    assert bot.boink_on() is True                     # unchanged
    assert any("currently ON" in s for s in fb.sent), fb.sent
    assert any("query" in s.lower() for s in fb.sent), fb.sent
    bot.set_boink(False)


async def test_boink_needs_no_companion_bot():
    # Unlike nostall (needs the guard bot installed), BOINK is dumb and self-contained: it must
    # turn on with NO other bot on disk.
    fb = FakeBot()
    orig = bot.discover_bots
    bot.discover_bots = lambda: {}                    # empty roster
    try:
        ctx = types.SimpleNamespace(bot=fb)
        await bot.maybe_handle_bot_command(ctx, 1, None, "bot boink on")
        assert bot.boink_on() is True
        assert not any("isn't installed" in s for s in fb.sent), fb.sent
    finally:
        bot.discover_bots = orig
        bot.set_boink(False)


async def test_boink_poke_enqueues_bare_BOINK_and_respects_cooldown():
    # THE BEHAVIOR: on a real stop, the watchdog pokes the bot with a message that is EXACTLY
    # "BOINK" — no manual, no instructions (the meaning lives in the bot). And the cooldown
    # stops a second poke inside the window, so one stop = one poke.
    sess = make_fake_session("claude")
    bot.registry.sessions["claude"] = sess
    wd = bot.Watchdog.__new__(bot.Watchdog)
    wd.session = sess
    wd.app = types.SimpleNamespace(bot=FakeBot())
    wd._boink_last = 0.0
    wd._chat = lambda: 12345

    async def _noop_show(body):
        return None
    wd._show = _noop_show

    poked = await wd._boink_poke()
    assert poked is True
    assert [m["text"] for m in sess.pending] == ["BOINK"]   # bare, exactly
    assert sess.pending[0]["source"] == "text"

    # Immediate second call is inside the cooldown -> no second poke, no second enqueue.
    poked2 = await wd._boink_poke()
    assert poked2 is False
    assert [m["text"] for m in sess.pending] == ["BOINK"]


async def test_boink_poke_fires_even_after_no_more_work():
    # BOINK is dumber than nostall on purpose: a declared NO-MORE-WORK does NOT silence it, since
    # the whole point is that a mind does not stop. The poke helper doesn't consult is_no_more_work
    # at all — proven here by poking a session that has declared done.
    sess = make_fake_session("claude")
    sess.no_more_work = True
    bot.registry.sessions["claude"] = sess
    wd = bot.Watchdog.__new__(bot.Watchdog)
    wd.session = sess
    wd.app = types.SimpleNamespace(bot=FakeBot())
    wd._boink_last = 0.0
    wd._chat = lambda: 12345

    async def _noop_show(body):
        return None
    wd._show = _noop_show

    assert await wd._boink_poke() is True
    assert [m["text"] for m in sess.pending] == ["BOINK"]
