import bot


def test_naked_when_no_bot_dir():
    p = bot.build_prompt("hi", "text", bot_name="ily")
    assert bot.bot_home("ily") is None
    assert "main.md" not in p
    assert p.startswith(bot.GUARD_TEXT)


def test_pointer_when_bot_defined():
    for name in ("blu", "gol"):
        assert bot.bot_home(name) is not None
        p = bot.build_prompt("hi", "text", bot_name=name)
        assert f"bots/{name}/main.md" in p
        assert name in p
        assert p.startswith(bot.GUARD_TEXT)


def test_pointer_absent_without_bot_name():
    assert bot.build_prompt("hi", "text") == bot.build_prompt("hi", "text", bot_name=None)
    assert "main.md" not in bot.build_prompt("hi", "text")
