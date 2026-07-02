"""Multi-session multiplexing: registry, per-session routing (concurrent), badges,
teardown. No Telegram, no Claude — routing is proven by a stubbed dispatch recorder."""

import asyncio

import bot
from tests.fakes import FakeApp, make_fake_session


def test_default_is_single_session_untagged():
    reg = bot.registry
    assert list(reg.sessions) == ["claude"]
    assert reg.multiplexing() is False
    assert reg.badge(reg.current()) == ""          # progressive disclosure: no tag
    assert bot.controller is reg.current().controller


def test_palette_is_nine_named():
    assert len(bot.SESSION_PALETTE) == 9
    assert [n for n, _ in bot.SESSION_PALETTE] == \
        ["claude", "blu", "gil", "ava", "ily", "max", "gol", "nyx", "sno"]
    assert bot.SESSION_EMOJI["gil"] == "🟢" and bot.SESSION_EMOJI["nyx"] == "⚫"


async def test_select_creates_switches_and_turns_on_tagging():
    reg = bot.registry
    bot._app = FakeApp()
    s = bot.select_session("gil")
    assert reg.multiplexing() is True
    assert s.name == "gil" and s.emoji == "🟢"
    assert reg.badge(s) == "🟢 gil · "
    assert reg.badge(reg.get("claude")) == "🟠 claude · "   # tags on for ALL now
    assert reg.current_name == "gil"
    assert bot.controller is s.controller                   # module global tracks current


def test_unknown_session_name_rejected():
    assert not bot.registry.known("zzz")
    assert all(bot.registry.known(n) for n in
               ("blu", "gil", "ava", "ily", "max", "gol", "nyx", "sno"))


async def test_routing_is_per_session_and_concurrent():
    reg = bot.registry
    bot._app = FakeApp()
    calls = []
    orig = bot.dispatch_to_claude

    async def rec(context, session, chat_id, reply_to, user_text, source,
                  raw=False, header="", voiceback=False):
        calls.append((session.name, user_text))
    bot.dispatch_to_claude = rec
    try:
        bot._activate_session(reg.current())          # claude worker
        bot.select_session("gil")                     # gil worker (activated on create)
        bot.enqueue_for_claude(reg.get("gil"), 1, None, "build", "text", False)
        bot.enqueue_for_claude(reg.get("claude"), 1, None, "lint", "text", False)
        await asyncio.sleep(0.25)                      # debounce is 0.02 in tests
        assert sorted(calls) == [("claude", "lint"), ("gil", "build")], calls
    finally:
        bot.dispatch_to_claude = orig


async def test_no_more_work_flag_is_per_session():
    bot._app = FakeApp()
    bot.select_session("gil")
    reg = bot.registry
    bot.set_no_more_work(reg.get("gil"), True)
    assert bot.is_no_more_work(reg.get("gil")) is True
    assert bot.is_no_more_work(reg.get("claude")) is False   # NOT global — no bleed


async def test_end_session_tears_down_and_untags():
    reg = bot.registry
    bot._app = FakeApp()
    bot.select_session("gil")
    assert reg.multiplexing() is True
    status = await bot.end_session("gil")
    assert "gil" in status
    assert reg.multiplexing() is False
    assert reg.current_name == "claude"                 # fell back to default
    assert reg.badge(reg.get("claude")) == ""           # tagging off again


async def test_cannot_end_default_session():
    msg = await bot.end_session("claude")
    assert "can't" in msg.lower()
    assert "claude" in bot.registry.sessions


async def test_end_session_teardown_and_default_guard():
    # the mechanism auto-end uses: tear down a named session, never the default.
    bot._app = FakeApp()
    bot.registry.sessions["gil"] = make_fake_session("gil")
    await bot.end_session("gil")
    assert "gil" not in bot.registry.sessions
    assert "default" in (await bot.end_session("claude")).lower()
    assert "no live session" in (await bot.end_session("zzz")).lower()
