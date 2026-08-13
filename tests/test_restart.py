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


def test_orphan_protection_noop_without_supervisor():
    # Headless (no marker): arming must be a silent no-op — the bot may outlive its shell.
    old = os.environ.pop("CLAUDEGRAM_SUPERVISED", None)
    try:
        bot._arm_orphan_protection()   # no prctl, no SystemExit
    finally:
        if old is not None:
            os.environ["CLAUDEGRAM_SUPERVISED"] = old


def test_orphan_protection_arms_pdeathsig_under_supervisor():
    # Supervised: the bot must ask the kernel to kill it when the tray dies (PR_SET_PDEATHSIG=1,
    # SIGTERM) — that's what makes a duplicate/orphan bot.py impossible. Fake libc: no real effect.
    import ctypes
    import signal

    calls = []

    class _FakeLibc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    old = os.environ.get("CLAUDEGRAM_SUPERVISED")
    os.environ["CLAUDEGRAM_SUPERVISED"] = "1"
    real_cdll = ctypes.CDLL
    ctypes.CDLL = lambda *a, **k: _FakeLibc()
    try:
        bot._arm_orphan_protection()
        assert calls, "prctl was never called"
        assert calls[0][0] == 1 and calls[0][1] == signal.SIGTERM, calls   # PR_SET_PDEATHSIG=1
    finally:
        ctypes.CDLL = real_cdll
        if old is None:
            del os.environ["CLAUDEGRAM_SUPERVISED"]
        else:
            os.environ["CLAUDEGRAM_SUPERVISED"] = old
