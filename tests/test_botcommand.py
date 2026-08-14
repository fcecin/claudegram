"""Regression: a message that merely OPENS with 'bot' (extremely common by voice — "Bot, can
you…") must NOT be swallowed as an unknown command and silently dropped. Long/prose 'bot …' now
falls through to Claude (maybe_handle_bot_command returns False); only a short, command-shaped
attempt still gets the "unknown command" hint. Real commands are unchanged."""

import types

import bot
from tests.fakes import FakeBot


def _ctx(fb):
    return types.SimpleNamespace(bot=fb)


async def test_prose_opening_with_bot_falls_through_to_claude():
    # The exact incident: a 95s voice note transcribed as "Bot cg4 is now shared. Can you check…"
    # was eaten by the command parser and never reached Claude. It must fall through now.
    fb = FakeBot()
    prose = ("Bot cg4 is now shared. Can you check if that's gonna work for me in Telegram and "
             "the other user as well, simultaneously? It looks weird.")
    handled = await bot.maybe_handle_bot_command(_ctx(fb), 1, None, prose)
    assert handled is False, "prose was swallowed as a command"      # → forwarded to Claude
    assert fb.sent == [], fb.sent                                    # and NO 'unknown command' reply


async def test_short_unknown_command_still_errors():
    # A short, command-shaped typo still gets the helpful hint (not forwarded to Claude).
    fb = FakeBot()
    handled = await bot.maybe_handle_bot_command(_ctx(fb), 1, None, "bot slect nyx")
    assert handled is True
    assert any("unknown" in s.lower() for s in fb.sent), fb.sent


async def test_real_command_still_handled():
    fb = FakeBot()
    handled = await bot.maybe_handle_bot_command(_ctx(fb), 1, None, "bot ping")
    assert handled is True
    assert any("pong" in s.lower() for s in fb.sent), fb.sent


async def test_message_not_opening_with_bot_is_never_a_command():
    fb = FakeBot()
    handled = await bot.maybe_handle_bot_command(_ctx(fb), 1, None, "hello there, how are you")
    assert handled is False
    assert fb.sent == [], fb.sent
