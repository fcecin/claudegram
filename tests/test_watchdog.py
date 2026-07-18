import bot
from tests.fakes import FakeApp, FakeBot, make_fake_session


def test_idle_balloons_dont_invalidate_across_sessions():
    # Several sessions on the 60s idle poll must each tick their OWN balloon (×N), not
    # leapfrog: one watchdog posting does not bury another session's balloon.
    a = make_fake_session("claude")
    b = make_fake_session("gil")
    bot.registry.sessions["claude"] = a
    bot.registry.sessions["gil"] = b
    a.watchdog = bot.Watchdog(FakeApp(), a)
    b.watchdog = bot.Watchdog(FakeApp(), b)
    a.watchdog.is_latest = True
    b.watchdog.is_latest = True

    a.watchdog._touch()               # a ticks its balloon
    assert a.watchdog.is_latest is True
    assert b.watchdog.is_latest is True   # b's balloon is NOT invalidated

    bot.mark_sent()                   # real content buries every balloon
    assert a.watchdog.is_latest is False
    assert b.watchdog.is_latest is False


async def test_failed_edit_keeps_the_idle_count():
    # A transient edit failure (e.g. flood control) is a delivery hiccup, not a status
    # change: the fresh fallback message must carry the ACCUMULATED ×N, not reset to 1 —
    # resetting silently pushed out the idle thresholds the nudges/auto-end key on.
    class _EditFailBot(FakeBot):
        async def edit_message_text(self, text, **kw):
            raise RuntimeError("Flood control exceeded")

    app = FakeApp()
    app.bot = _EditFailBot()
    sess = make_fake_session("claude")
    bot.registry.sessions["claude"] = sess
    saved = bot.ALLOWED_USER_IDS
    bot.ALLOWED_USER_IDS = [7]
    try:
        wd = bot.Watchdog(app, sess)
        await wd._show("💤 idle · 🐚 no shells — nothing running.")   # fresh message, ×1
        assert wd.count == 1 and len(app.bot.sent) == 1
        await wd._show("💤 idle · 🐚 no shells — nothing running.")   # same body → edit fails
        assert wd.count == 2                       # count survived the failed edit
        assert len(app.bot.sent) == 2
        assert "×2" in app.bot.sent[-1]            # and the fresh message carries it
    finally:
        bot.ALLOWED_USER_IDS = saved


async def test_not_modified_edit_is_treated_as_success():
    # "Message is not modified" means the balloon already shows this text — falling
    # through re-posted the SAME status as a brand-new message. Treat it as success.
    class _NotModifiedBot(FakeBot):
        async def edit_message_text(self, text, **kw):
            raise RuntimeError("Message is not modified")

    app = FakeApp()
    app.bot = _NotModifiedBot()
    sess = make_fake_session("claude")
    bot.registry.sessions["claude"] = sess
    saved = bot.ALLOWED_USER_IDS
    bot.ALLOWED_USER_IDS = [7]
    try:
        wd = bot.Watchdog(app, sess)
        await wd._show("💤 idle · 🐚 no shells — nothing running.")
        first_mid = wd.msg_id
        await wd._show("💤 idle · 🐚 no shells — nothing running.")
        assert len(app.bot.sent) == 1              # NOT re-posted
        assert wd.msg_id == first_mid              # same balloon
        assert wd.count == 2                       # the tick still counted
    finally:
        bot.ALLOWED_USER_IDS = saved
