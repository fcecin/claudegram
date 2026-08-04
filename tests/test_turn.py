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


def test_usage_limit_vs_rate_limit_discrimination():
    # The two states must never be confused: a HARD usage cap parks; a transient 429 retries.
    assert bot.is_usage_limited("You've hit your weekly limit · resets 7pm (America/Sao_Paulo)")
    assert bot.is_usage_limited("You've hit your limit · resets 3pm (America/Sao_Paulo)")
    assert bot.is_usage_limited("Claude usage limit reached — try later")
    assert not bot.is_usage_limited("overloaded_error: upstream is busy")
    assert not bot.is_usage_limited("here is my normal answer, no limits hit")
    assert not bot.is_usage_limited(None)
    # A transient marker must NOT read as a hard cap, and the hard-cap text must NOT read as transient.
    assert bot.is_rate_limited("overloaded_error") and not bot.is_usage_limited("overloaded_error")
    assert (bot.is_usage_limited("You've hit your weekly limit")
            and not bot.is_rate_limited("You've hit your weekly limit"))


def test_fable_exhaustion_detection():
    # Fable exhaustion is its OWN state: it must be recognized, yet must NOT read as either a
    # transient throttle (would wait 5 min) or a hard subscription cap (would park) — so the
    # dispatcher falls back to Opus instead of waiting or parking.
    fable = ("You've reached your Fable 5 limit. Run /usage-credits to continue or "
             "switch models with /model.")
    assert bot.is_fable_exhausted(fable)
    assert not bot.is_fable_exhausted("here is a fable with a moral, no limits involved")
    assert not bot.is_fable_exhausted("You've hit your weekly limit · resets 7pm")
    assert not bot.is_fable_exhausted(None)
    assert not bot.is_rate_limited(fable)       # not transient throttling
    assert not bot.is_usage_limited(fable)      # not a hard subscription cap


async def test_hard_usage_limit_parks_and_does_not_retry():
    # The reported bug: a weekly-cap crash was (a) retried 5x with a false "NOT your usage limit"
    # notice, then (b) re-driven by the idle nudger every ~30 min into an all-day crash loop.
    # Now it must PARK on the first hit — no retry, no generic crash notice.
    fb = FakeBot()
    sess = make_fake_session("claude", script=[
        result_msg(is_error=True, subtype="success",
                   result="You've hit your weekly limit · resets 7pm (America/Sao_Paulo)")])
    await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "do a thing", "text")
    assert sess.parked is True                                          # parked, not looping
    joined = "\n".join(fb.sent)
    assert "weekly limit" in joined                                     # the reason is shown
    assert "PARKED" in joined                                           # and that we parked
    assert not any("Retrying" in s or "attempt" in s for s in fb.sent), fb.sent   # never a retry
    assert not any("That turn crashed" in s for s in fb.sent), fb.sent            # not the generic crash


async def test_transient_throttle_retries_not_parks():
    # The other side of the fork: a genuine transient 429/overload still retries and recovers,
    # and must NOT be mistaken for a hard cap (no park).
    fb = FakeBot()
    saved = bot.RATE_LIMIT_RETRY_SECS
    bot.RATE_LIMIT_RETRY_SECS = 0          # don't actually wait 5 min in a test
    try:
        sess = make_fake_session("claude", scripts=[
            [result_msg(is_error=True, subtype="error", result="overloaded_error: upstream overloaded")],
            [stream_text("recovered"), result_msg(result="recovered")]])
        await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "hi", "text")
        assert sess.parked is False                                     # transient is not a park
        assert any("Retrying" in s for s in fb.sent), fb.sent           # it announced a retry
        assert "recovered" in fb.sent                                   # and eventually succeeded
    finally:
        bot.RATE_LIMIT_RETRY_SECS = saved


async def test_fable_exhausted_autoswitches_to_opus_and_retries():
    # The feature: a turn running on Fable that hits Fable's capacity cap must auto-switch the
    # session's model to Opus and retry IMMEDIATELY (no 5-min wait, no park), then recover — so a
    # Fable-exhausted bot heals itself instead of crash-looping every idle-nudge cycle.
    fb = FakeBot()
    sess = make_fake_session("claude", scripts=[
        [result_msg(is_error=True, subtype="success",
                    result="You've reached your Fable 5 limit. Run /usage-credits to continue "
                           "or switch models with /model.")],
        [stream_text("recovered on opus"), result_msg(result="recovered on opus")]])
    await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "hi", "text")
    assert sess.parked is False                                          # fallback, not a park
    assert sess.controller.model_switches == ["opus"], sess.controller.model_switches  # switched to Opus
    assert any("Fable" in s and "opus" in s.lower() for s in fb.sent), fb.sent   # user told why
    assert "recovered on opus" in fb.sent                               # retried and recovered
    assert not any("crashed" in s.lower() for s in fb.sent), fb.sent    # never surfaced as a crash


async def test_fable_fallback_is_one_shot_no_loop():
    # Guard rail: if Opus ALSO errors right after the fallback, we must NOT keep switching/looping —
    # exactly one switch, then normal crash handling takes over.
    fb = FakeBot()
    sess = make_fake_session("claude", scripts=[
        [result_msg(is_error=True, subtype="success",
                    result="You've reached your Fable 5 limit. Switch models with /model.")],
        [result_msg(is_error=True, subtype="error", result="opus kaboom")]])
    await bot.dispatch_to_claude(_ctx(fb), sess, 1, None, "hi", "text")
    assert sess.controller.model_switches == ["opus"], sess.controller.model_switches  # exactly one switch
    assert any("crashed" in s.lower() for s in fb.sent), fb.sent        # the Opus error surfaces normally


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
