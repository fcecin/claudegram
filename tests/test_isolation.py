"""Guard against a footgun that bit us live: running the offline suite in the repo dir used to
toggle the RUNNING install's state, because clear_flags() unlinks the presence-flag files at
their real module paths (HERE/nostall.mode, …). One `./test.sh` silently turned off a live
anti-stall guard — and would just as happily clear a live BLOCKED/SLEEP flag. fakes.py now
repoints every writable flag at a throwaway temp dir; this test pins that so it can't regress.
"""
import pathlib

import bot


def test_writable_flags_are_redirected_out_of_the_repo():
    repo = pathlib.Path(bot.HERE).resolve()
    for f in (bot.NOSTALL_FILE, bot.BLOCK_FILE, bot.SLEEP_FILE,
              bot.VOICE_MODE_FILE, bot.INTRUSION_OFF_FILE):
        p = pathlib.Path(f).resolve()
        assert repo != p and repo not in p.parents, f"{f} still resolves inside the repo"


def test_clear_flags_cannot_touch_a_live_repo_flag():
    # Simulate a live nostall.mode in the real install and prove clear_flags leaves it alone.
    live = pathlib.Path(bot.HERE).resolve() / "nostall.mode"
    preexisting = live.exists()
    if not preexisting:
        live.write_text("on", encoding="utf-8")
    try:
        from tests import fakes
        fakes.clear_flags()
        assert live.exists(), "clear_flags() wiped the live install's nostall.mode"
    finally:
        if not preexisting:
            live.unlink()
