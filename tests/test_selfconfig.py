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
    c = bot.ClaudeController("/tmp/cg-selfcfg-test", "/tmp/cg-selfcfg-test.id")
    assert await c.set_model("haiku")
    assert c.forced_model == "haiku"
    assert await c.set_model(None)
    assert c.forced_model is None


def test_selfconfig_preamble_bakes_in_the_bot_identity():
    # Each bot is taught cg-cmd WITH its own name, so under multiplexing "manage yourself"
    # targets the issuing bot — never whichever session the user has selected.
    assert "--as gil" in bot.build_prompt("hi", "text", bot_name="gil")
    assert "--as" not in bot.selfconfig_preamble(None)   # anonymous/manual form unchanged


async def test_selfconfig_targets_the_issuing_bot_not_current():
    # THE bug this pins: a background bot's cg-cmd used to act on the CURRENT session.
    # Current stays 'claude'; a drop tagged '@gil' must configure gil and only gil.
    gil = bot.Session("gil")
    bot.registry.sessions["gil"] = gil
    claude = bot.registry.get("claude")
    claude_model_before = claude.controller.forced_model
    fb = FakeBot()
    await bot._run_selfconfig(fb, 1, "@gil park")
    assert gil.parked is True
    assert claude.parked is False                        # the selected bot is untouched
    fb2 = FakeBot()
    await bot._run_selfconfig(fb2, 1, "@gil model haiku")
    assert gil.controller.forced_model == "haiku"
    assert claude.controller.forced_model == claude_model_before
    assert any("gil" in s for s in fb.sent)              # attribution names the issuer


async def test_selfconfig_status_reports_the_issuing_bot():
    gil = bot.Session("gil")
    bot.registry.sessions["gil"] = gil                   # 2 sessions -> multiplexing on
    fb = FakeBot()
    await bot._run_selfconfig(fb, 1, "@gil status")
    assert any("bot:" in s and "gil" in s for s in fb.sent), fb.sent


async def test_selfconfig_from_unknown_session_is_refused():
    # An identity that names no LIVE session must be refused — never fall through and land
    # the command on somebody else.
    fb = FakeBot()
    await bot._run_selfconfig(fb, 1, "@ghost park")
    assert any("no such live session" in s for s in fb.sent), fb.sent
    assert bot.registry.get("claude").parked is False


async def test_selfconfig_bare_drop_still_targets_current():
    # Legacy/manual drops without --as keep the old behavior: current session.
    fb = FakeBot()
    await bot._run_selfconfig(fb, 1, "park")
    assert bot.registry.get("claude").parked is True


def test_preamble_email_gated_and_compact():
    # Email teaching is folded into the ONE helpers preamble and appears IFF a resend.key
    # exists; and the whole recurring overhead stays lean — it rides EVERY turn, so bloat
    # here is a per-turn token tax (the 2026-07-18 trim took it from ~1600 to ~1200 chars).
    import pathlib
    import tempfile
    saved = bot.RESEND_KEY_FILE
    td = pathlib.Path(tempfile.mkdtemp(prefix="cg-key-"))
    bot.RESEND_KEY_FILE = td / "resend.key"
    try:
        p_off = bot.build_prompt("X", "text", bot_name="gil")
        assert "cg-mail" not in p_off                     # no key -> email never taught
        bot.RESEND_KEY_FILE.write_text("k", encoding="utf-8")
        p_on = bot.build_prompt("X", "text", bot_name="gil")
        assert "cg-mail" in p_on and "TYPED" in p_on      # key -> taught, with the typed-address rule
        assert "cg-send" in p_on and "--as gil" in p_on   # the merged block keeps every helper
        assert len(p_on) - 1 < 1350, len(p_on)            # anti-bloat budget for the full overhead
    finally:
        bot.RESEND_KEY_FILE = saved
