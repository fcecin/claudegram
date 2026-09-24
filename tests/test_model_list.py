"""Model labels show family + exact version, and `bot list models` enumerates what is
selectable (family aliases always; concrete versions scraped from the installed CLI, never
a metered-API call)."""

import contextlib
import os
import types

import bot
from tests.fakes import FakeBot

FAMILIES = ("opus", "sonnet", "haiku", "fable")


@contextlib.contextmanager
def _ambient(model):
    old = os.environ.get("ANTHROPIC_MODEL")
    os.environ["ANTHROPIC_MODEL"] = model
    try:
        yield
    finally:
        if old is None:
            del os.environ["ANTHROPIC_MODEL"]
        else:
            os.environ["ANTHROPIC_MODEL"] = old


def _ctrl(forced=None, model=None):
    return types.SimpleNamespace(forced_model=forced, model=model)


def test_model_family_maps_ids_and_aliases():
    assert bot._model_family("claude-opus-4-8-20250101") == "opus"
    assert bot._model_family("claude-fable-5-1") == "fable"
    assert bot._model_family("claude-sonnet-4-5") == "sonnet"
    assert bot._model_family("claude-haiku-4-5") == "haiku"
    assert bot._model_family("opus") == "opus"
    assert bot._model_family("something-else") == "something-else"


def test_label_forced_alias_without_a_resolved_version():
    assert bot._model_label(_ctrl(forced="opus")) == "opus"
    assert bot._model_label(_ctrl(forced="fable")) == "fable"


def test_label_forced_alias_shows_family_and_exact_version():
    assert bot._model_label(_ctrl(forced="opus", model="claude-opus-4-8-20250101")) \
        == "opus (claude-opus-4-8-20250101)"


def test_label_ignores_a_stale_resolved_version_from_another_family():
    # switched to fable but ctrl.model still holds the previous opus id -> show just 'fable'
    assert bot._model_label(_ctrl(forced="fable", model="claude-opus-4-8-20250101")) == "fable"


def test_label_forced_full_id_shows_family_and_the_pinned_id():
    assert bot._model_label(_ctrl(forced="claude-opus-5-5")) == "opus (claude-opus-5-5)"


def test_label_unforced_shows_default_family():
    with _ambient("opus"):
        assert bot._model_label(_ctrl()) == "default: opus"
        assert bot._model_label(_ctrl(model="claude-opus-4-8-20250101")) \
            == "default: opus (claude-opus-4-8-20250101)"


def test_installed_model_ids_are_clean_family_ids():
    ids = bot._installed_model_ids()
    assert isinstance(ids, list)
    for i in ids:
        assert i.startswith("claude-")
        assert not i.endswith("-v1") and "mythos" not in i
        assert bot._model_family(i) in FAMILIES


def test_format_model_list_always_offers_the_family_aliases():
    text = bot._format_model_list()
    for fam in FAMILIES:
        assert fam in text
    assert "opusplan" in text
    assert "Set with: bot model" in text


async def test_bot_list_models_command_variants():
    for cmd in ("bot list models", "bot models", "bot list", "bot model list", "bot model models"):
        fb = FakeBot()
        assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb), 1, None, cmd)
        joined = "\n".join(fb.sent)
        assert "Set with: bot model" in joined, (cmd, fb.sent)
        for fam in FAMILIES:
            assert fam in joined, (cmd, fam)


async def test_bare_bot_model_still_shows_current_not_the_list():
    fb = FakeBot()
    assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb), 1, None, "bot model")
    joined = "\n".join(fb.sent)
    assert "🧠 Model:" in joined, fb.sent
    assert "Selectable models" not in joined, fb.sent
