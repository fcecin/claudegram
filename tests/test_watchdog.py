import bot
from tests.fakes import FakeApp, make_fake_session


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
