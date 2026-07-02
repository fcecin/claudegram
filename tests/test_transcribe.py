import bot


def test_sno_uses_fast_codec():
    assert bot.session_compute(bot.Session("sno")) == "int8"


def test_default_bot_uses_global_codec():
    assert bot.session_compute(bot.Session("claude")) == bot.get_compute_type()
