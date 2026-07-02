import bot


def test_alias_resolution():
    cases = {
        "nix": "nyx", "nikes": "nyx", "knicks": "nyx", "nicks": "nyx",
        "bloo": "blu", "blue": "blu",
        "guil": "gil", "guile": "gil", "jill": "gil", "jil": "gil", "gill": "gil",
        "ili": "ily", "elle": "ily", "elly": "ily", "illy": "ily",
        "cloud": "claude", "clod": "claude", "claudee": "claude",
        "gall": "gol", "gou": "gol", "goo": "gol", "goal": "gol",
        "snow": "sno", "maks": "max", "mx": "max",
    }
    for raw, canon in cases.items():
        assert bot.resolve_session_name(raw) == canon, (raw, bot.resolve_session_name(raw))


def test_canonical_names_resolve_to_self():
    for n in bot.selectable_bots():
        assert bot.resolve_session_name(n) == n


def test_internal_bots_are_not_selectable():
    # The anti-stall bot exists on disk and is internal, but must NEVER resolve via `bot select`
    # — it is driven internally, not chosen by the user.
    assert bot.NOSTALL_BOT in bot.discover_bots()
    assert bot.resolve_session_name(bot.NOSTALL_BOT) is None


def test_unknown_and_empty_return_none():
    assert bot.resolve_session_name("zztop") is None
    assert bot.resolve_session_name("") is None
    assert bot.resolve_session_name(None) is None
