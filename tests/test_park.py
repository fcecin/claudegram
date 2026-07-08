import types

import bot
from tests.fakes import FakeBot


async def test_bot_park_sets_flag_and_replies():
    fb = FakeBot()
    cur = bot.registry.current()
    cur.parked = False
    assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb), 1, None, "bot park")
    assert cur.parked is True
    assert any("Parked" in s for s in fb.sent), fb.sent


async def test_user_input_unparks():
    # A real user text turn re-arms the nudger AND un-parks the session (same handler spot).
    sess = bot.registry.current()
    sess.parked = True
    sess.no_more_work = True

    async def _noop(*a, **k):
        pass

    # Use an allowed id (the test env may load a real allowlist from .env) so it isn't an intruder.
    uid = next(iter(bot.ALLOWED_USER_IDS)) if bot.ALLOWED_USER_IDS else 1
    user = types.SimpleNamespace(id=uid, full_name="test")
    msg = types.SimpleNamespace(text="hi", chat_id=uid, message_id=1,
                                from_user=user, reply_text=_noop)
    update = types.SimpleNamespace(message=msg, effective_user=user)
    await bot.handle_text(update, types.SimpleNamespace(bot=FakeBot()))
    assert sess.parked is False
    assert sess.no_more_work is False


def test_parked_flag_defaults_off():
    assert bot.Session("claude").parked is False


async def test_bot_can_park_itself_via_selfconfig():
    # A bot parks itself through the self-config channel (./cg-cmd park), not only the user.
    fb = FakeBot()
    cur = bot.registry.current()
    cur.parked = False
    await bot._run_selfconfig(fb, 1, "park")
    assert cur.parked is True
    assert not any("refused" in s for s in fb.sent), fb.sent
