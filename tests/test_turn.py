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


async def test_stuck_release_reports_stuck_not_done():
    # ask()'s silence net released the turn (no result). The old path fell through to
    # finalize() and posted "✅ Done · ? turns · 0s" — a fake success. It must instead free
    # the prompt and say the turn went silent.
    fb = FakeBot()
    sess = make_fake_session("claude", script=[stream_text("partial answer, then silence")])
    sess.controller._stuck_release = True     # as if the 900s net fired
    await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "hi", "text")
    allmsgs = fb.sent + fb.edited
    assert not any(s.startswith("✅ Done") for s in fb.sent), fb.sent   # never a fake Done
    assert "[[END]]" in fb.sent                                        # prompt still freed
    assert any("silent" in s.lower() or "stuck" in s.lower() for s in allmsgs), allmsgs


async def test_spontaneous_stray_result_is_ignored():
    # A stray ResultMessage with no open segment (e.g. a late turn-end after a stuck
    # release) must not open a board just to slam it shut ("picked back up… ✅ Done").
    saved = bot.ALLOWED_USER_IDS
    bot.ALLOWED_USER_IDS = [123]
    try:
        app = FakeApp()
        sess = make_fake_session("claude")
        relay = bot.SpontaneousRelay(app, sess)
        sess.controller.set_spontaneous_handler(relay.on_message)
        await sess.controller.push_spontaneous([result_msg()])
        assert app.bot.sent == [], app.bot.sent
    finally:
        bot.ALLOWED_USER_IDS = saved
