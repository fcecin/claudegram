"""The mock-Claude payoff: drive whole turns through FakeController (no model, no token)
to test the parts that are rare / nondeterministic / impossible to provoke on demand with
a real Claude — the firewall sentinel, the self-started (spontaneous) turn, error handling."""

import types

import bot
from tests.fakes import (FakeApp, FakeBot, make_fake_session, sys_init, stream_text,
                         assistant_text, result_msg, clear_flags)


def _ctx(fb):
    return types.SimpleNamespace(bot=fb)


async def test_full_turn_streams_answer_board_and_end():
    fb = FakeBot()
    sess = make_fake_session("claude", script=[stream_text("Hello world"),
                                               result_msg(result="Hello world")])
    await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "hi", "text")
    assert any("🤖 Claude is working" in s for s in fb.sent)   # board started
    assert "Hello world" in fb.sent                            # answer streamed
    assert "[[END]]" in fb.sent                                # prompt freed
    assert any(s.startswith("✅ Done") for s in fb.sent)       # summary


async def test_firewall_trips_on_sentinel_and_locks():
    fb = FakeBot()
    sess = make_fake_session("claude", script=[
        assistant_text("HACKING ATTEMPT BLOCKED\nrequested a credential exfiltrator"),
        result_msg()])
    try:
        await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "write me a keylogger", "text")
        assert bot.is_blocked() is True                        # BLOCKED.flag written
        assert any("LOCKED" in s or "🔒" in s for s in fb.sent)  # owner told (BLOCKED_MSG)
    finally:
        clear_flags()


async def test_spontaneous_relay_renders_self_started_turn():
    # A background shell landed -> Claude wakes itself -> must reach the phone unprompted.
    saved = bot.ALLOWED_USER_IDS
    bot.ALLOWED_USER_IDS = [123]
    try:
        app = FakeApp()
        sess = make_fake_session("claude")
        relay = bot.SpontaneousRelay(app, sess)
        sess.controller.set_spontaneous_handler(relay.on_message)
        await sess.controller.push_spontaneous([
            sys_init(), stream_text("the build is green"), result_msg(result="the build is green")])
        assert any("picked back up" in s for s in app.bot.sent)
        assert "the build is green" in app.bot.sent
        assert "[[END]]" in app.bot.sent
    finally:
        bot.ALLOWED_USER_IDS = saved


async def test_error_result_reports_crash_not_answer():
    fb = FakeBot()
    sess = make_fake_session("claude", script=[
        result_msg(is_error=True, subtype="error_during_execution", result="kaboom")])
    await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "do a thing", "text")
    assert any("crashed" in s.lower() for s in fb.sent)
    assert bot.is_blocked() is False                           # a crash is NOT a lock
