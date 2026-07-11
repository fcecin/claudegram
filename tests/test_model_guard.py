"""The fable guard: fable is never an AMBIENT default — a session runs it only when
explicitly set (bots/<name>/config.json "model" or `bot model fable`). If the
machine's env/settings default resolves to fable, unforced sessions get the
fallback (opus) passed as an explicit --model instead."""

import contextlib
import os

import bot
from claude_driver import FABLE_GUARD_FALLBACK, default_model_guard


@contextlib.contextmanager
def _ambient(model):
    """Pin the ambient default via ANTHROPIC_MODEL (it outranks settings.json,
    so the machine's real ~/.claude/settings.json can't leak into the test)."""
    old = os.environ.get("ANTHROPIC_MODEL")
    os.environ["ANTHROPIC_MODEL"] = model
    try:
        yield
    finally:
        if old is None:
            del os.environ["ANTHROPIC_MODEL"]
        else:
            os.environ["ANTHROPIC_MODEL"] = old


def test_guard_trips_on_any_fable_spelling():
    for ambient in ("fable", "Fable", "claude-fable-5"):
        with _ambient(ambient):
            assert default_model_guard() == FABLE_GUARD_FALLBACK, ambient


def test_guard_stays_out_of_the_way_otherwise():
    for ambient in ("opus", "claude-opus-4-8", "sonnet"):
        with _ambient(ambient):
            assert default_model_guard() is None, ambient


def test_unforced_session_never_spawns_with_fable_default():
    with _ambient("fable"):
        c = bot.ClaudeController("/tmp/cg-guard-test", "/tmp/cg-guard-test.id", None, None)
        assert c._build_options().model == FABLE_GUARD_FALLBACK
        # ...and the status label reports the effective default, not the ambient one
        assert bot.default_model() == FABLE_GUARD_FALLBACK


async def test_explicit_fable_passes_through_the_guard():
    with _ambient("fable"):
        c = bot.ClaudeController("/tmp/cg-guard-test", "/tmp/cg-guard-test.id", None, None,
                                 model="claude-fable-5")
        assert c._build_options().model == "claude-fable-5"
        # reverting to default re-engages the guard
        assert await c.set_model(None)
        assert c._build_options().model == FABLE_GUARD_FALLBACK
