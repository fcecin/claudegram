import types

import bot
from tests.fakes import FakeBot


def test_selfconfig_preamble_teaches_the_helper():
    p = bot.build_prompt("hi there", "text")
    assert "cg-cmd" in p
    assert "self-config" in p.lower()
    # names the safe knobs
    for word in ("effort", "model", "voice", "transcribe"):
        assert word in p


async def test_selfconfig_runs_allowed_readonly_command():
    fb = FakeBot()
    await bot._run_selfconfig(fb, 1, "status")
    # attributed to the bot, and the command actually ran (status text followed)
    assert any("self-config" in s for s in fb.sent), fb.sent
    assert any("🧠 model:" in s or "session:" in s for s in fb.sent), fb.sent


async def test_selfconfig_refuses_dangerous_commands():
    for bad in ("kill", "new", "clear", "sleep", "stop", "restart", "harness hi"):
        fb = FakeBot()
        await bot._run_selfconfig(fb, 1, bad)
        assert any("refused" in s for s in fb.sent), (bad, fb.sent)
        # the dangerous command's real handler must NOT have run
        assert not any("effort set" in s or "Model set" in s for s in fb.sent)


async def test_bot_model_command_sets_shows_and_resets():
    fb = FakeBot()
    ctx = types.SimpleNamespace(bot=fb)
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot model sonnet")
    assert bot.controller.forced_model == "sonnet"
    assert any("Model set to: sonnet" in s for s in fb.sent), fb.sent

    fb2 = FakeBot()
    assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb2), 1, None, "bot model default")
    assert bot.controller.forced_model is None

    fb3 = FakeBot()
    assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb3), 1, None, "bot model bogus")
    assert any("Unknown model" in s for s in fb3.sent), fb3.sent


async def test_bot_model_fable_maps_to_pinned_id():
    fb = FakeBot()
    ctx = types.SimpleNamespace(bot=fb)
    assert await bot.maybe_handle_bot_command(ctx, 1, None, "bot model fable")
    assert bot.controller.forced_model == "claude-fable-5"
    assert any("Model set to: claude-fable-5" in s for s in fb.sent), fb.sent
    # leave the module controller on the default model for later tests
    assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=FakeBot()), 1, None, "bot model default")
    assert bot.controller.forced_model is None


async def test_set_model_on_controller():
    c = bot.ClaudeController("/tmp/cg-selfcfg-test", "/tmp/cg-selfcfg-test.id", None, None)
    assert await c.set_model("haiku")
    assert c.forced_model == "haiku"
    assert await c.set_model(None)
    assert c.forced_model is None
