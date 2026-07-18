"""`bot restart` must work on BOTH launch paths: under the tray (which sets
CLAUDEGRAM_SUPERVISED and respawns an exited child — the proven path, unchanged) and
headless via ./run.sh, where the old unconditional os._exit(0) just stopped the bridge
for good. Headless now re-execs bot.py in place."""

import os

import bot


def test_restart_mode_supervised_exits_for_the_tray_to_respawn():
    old = os.environ.get("CLAUDEGRAM_SUPERVISED")
    os.environ["CLAUDEGRAM_SUPERVISED"] = "1"
    try:
        assert bot._restart_mode() == "exit"
    finally:
        if old is None:
            del os.environ["CLAUDEGRAM_SUPERVISED"]
        else:
            os.environ["CLAUDEGRAM_SUPERVISED"] = old


def test_restart_mode_headless_execs_in_place():
    old = os.environ.pop("CLAUDEGRAM_SUPERVISED", None)
    try:
        assert bot._restart_mode() == "exec"          # ./run.sh: exiting would be suicide
    finally:
        if old is not None:
            os.environ["CLAUDEGRAM_SUPERVISED"] = old


def test_gui_marks_the_bot_as_supervised():
    # The tray must actually SET the marker, or every restart would take the exec path.
    src = (bot.HERE / "gui.py").read_text(encoding="utf-8")
    assert 'env.insert("CLAUDEGRAM_SUPERVISED", "1")' in src
