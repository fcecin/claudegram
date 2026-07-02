import types

import bot
from tests.fakes import FakeBot, make_fake_session


def test_config_resolution():
    sno = bot.bot_config("sno")
    assert sno["model"] == "haiku" and sno["transcribe"] == "fast" and sno["effort"] == "medium"
    assert bot.bot_config("ily")["effort"] == "high"
    assert bot.bot_config("ava")["effort"] == "xhigh"
    nyx = bot.bot_config("nyx")
    assert nyx["model"] == "opus" and nyx["effort"] == "max" and nyx["voiceback"] is False
    assert bot.bot_config("blu")["effort"] == "xhigh"
    mx = bot.bot_config("max")
    assert mx["model"] == "opus" and mx["effort"] == "max" and mx["voice"] == {"name": "bm_fable"}
    assert bot.bot_config("gil")["transcribe"] == "good"
    # Every bot now carries its own icon in config (roster + badges are config-driven).
    for name in ("claude", "blu", "gil", "ava", "ily", "max", "gol", "nyx", "sno"):
        assert bot.bot_config(name).get("icon"), name


def test_forced_model_is_per_session():
    for name, model in (("sno", "haiku"), ("ily", "sonnet"), ("ava", "opus"),
                        ("nyx", "opus"), ("max", "opus")):
        assert bot.Session(name).controller.forced_model == model
    assert bot.Session("claude").controller.forced_model is None
    assert bot.Session("claude").controller.max_budget_usd is None


def test_forced_effort_from_config():
    for name, effort in (("ava", "xhigh"), ("ily", "high"), ("sno", "medium"), ("blu", "xhigh")):
        p = bot.HERE / f"effort.{name}.level"
        if p.exists():
            p.unlink()
        assert bot.Session(name).controller.effort == effort
    assert bot.Session("claude").controller.effort  # a concrete default


def test_model_bots_are_naked_without_main_md():
    for name in ("sno", "ily", "ava"):
        assert bot.bot_home(name) is None
        assert "main.md" not in bot.build_prompt("hi", "text", bot_name=name)


def test_max_bot():
    home = bot.bot_home("max")
    assert home is not None
    assert (home / "main.md").read_text().strip() == "Read var/main.md if available."
    assert bot.Session("max").controller.forced_model == "opus"
    assert bot.bot_config("max")["effort"] == "max"
    assert bot.bot_config("max").get("protect_settings") is None


def test_gil_stub():
    home = bot.bot_home("gil")
    assert home is not None
    assert (home / "main.md").read_text().strip() == "Read var/main.md if available."
    assert bot.Session("gil").controller.forced_model is None


async def test_empty_reply_mechanism():
    fb = FakeBot()
    sess = make_fake_session("claude")
    sess.empty_reply = "(nothing here)"
    sess.controller.raises = RuntimeError("model unavailable")
    await bot.dispatch_to_claude(types.SimpleNamespace(bot=fb), sess, 1, None, "hi", "text")
    shown = fb.sent + fb.edited
    assert any("nothing here" in s for s in shown), shown
