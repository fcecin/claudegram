import bot


def test_config_bot_pins_its_codec_at_spawn():
    for name in ("sno", "ily", "gol"):
        assert bot.session_compute(bot.Session(name)) == "int8", name          # config: fast
    for name in ("ava", "blu", "gil"):
        assert bot.session_compute(bot.Session(name)) == "int8_float32", name  # config: good


def test_default_bot_inherits_the_code_default_best():
    # No config => inherit the immutable code default (best/float32), NOT a persisted file.
    assert bot.get_compute_type() == bot.DEFAULT_COMPUTE == "float32"
    assert bot.session_compute(bot.Session("claude")) == "float32"


def test_spawn_values_come_from_config_or_code_defaults():
    claude = bot.Session("claude")          # no config
    assert claude.controller.effort == "high"          # code default
    assert claude.controller.forced_model is None      # Claude's own default
    assert claude.compute is None                       # inherit => best at read time
    sno = bot.Session("sno")
    assert sno.controller.forced_model == "haiku"       # from config


async def test_runtime_transcribe_is_per_bot_and_not_persisted(tmp_path=None):
    import types
    from tests.fakes import FakeBot
    fb = FakeBot()
    before_files = set(p.name for p in bot.HERE.glob("compute.type"))
    await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb), 1, None, "bot transcribe fast")
    cur = bot.registry.current()
    assert cur.compute == "int8"                         # this bot's live value changed
    assert bot.session_compute(cur) == "int8"
    # global default untouched, and nothing was written to disk
    assert bot.get_compute_type() == "float32"
    assert set(p.name for p in bot.HERE.glob("compute.type")) == before_files


async def test_runtime_effort_does_not_persist():
    c = bot.ClaudeController("/tmp/cg-effort-test", "/tmp/cg-effort-test.id", None, None)
    assert c.effort == "high"                            # code default at spawn
    assert await c.set_effort("low")
    assert c.effort == "low"                             # in-memory change
    # a fresh controller (== a restart) is back to the default, not "low"
    c2 = bot.ClaudeController("/tmp/cg-effort-test", "/tmp/cg-effort-test.id", None, None)
    assert c2.effort == "high"
