import bot


def test_voice_filters_empty_is_noop():
    assert bot._voice_filters({}) == "aresample=48000"


def test_voice_filters_pitch_and_robot():
    deep = bot._voice_filters({"pitch": -6})
    assert "asetrate=48000*0.70711" in deep and "atempo=1.41421" in deep
    assert "afftfilt" in bot._voice_filters({"robot": True})
    assert "aecho" in bot._voice_filters({"reverb": True})
    assert "bass=g=6" in bot._voice_filters({"bass": 6})


def test_resolve_voice_language_and_gender():
    # English keeps the bot's own voice; accent from prefix (a=US, b=UK)
    assert bot._resolve_voice("am_fenrir", "en") == ("am_fenrir", "en-us")
    assert bot._resolve_voice("bf_emma", "en") == ("bf_emma", "en-gb")
    # non-English swaps to a native voice, preserving gender
    assert bot._resolve_voice("am_fenrir", "pt") == ("pm_alex", "pt-br")
    assert bot._resolve_voice("af_bella", "pt") == ("pf_dora", "pt-br")
    assert bot._resolve_voice("bf_emma", "es") == ("ef_dora", "es")
    # unknown language: best-effort with the bot's own voice
    assert bot._resolve_voice("am_adam", "xx") == ("am_adam", "en-us")


def test_per_bot_voices_configured():
    assert bot.bot_config("max")["voice"]["name"] == "bm_fable"
    assert bot.bot_config("sno")["voice"]["name"] == "am_adam"
    assert bot.bot_config("ily")["voice"]["name"] == "bf_emma"
    assert bot.bot_config("ava")["voice"]["name"] == "af_bella"
    assert bot.bot_config("gol")["voice"]["robot"] is True
    assert bot.bot_config("blu")["voice"]["name"] == "am_michael"
    assert "voice" not in bot.bot_config("claude")  # -> DEFAULT_VOICE
