"""clipvoice must never outlive its terminal.

2026-08-11 incident: closing the terminal window left clipvoice running — the
input loop spun at 100% CPU strobing full-screen redraws into the dead pty,
whisper kept decoding, and the mic capture stayed open. Two exit paths are
pinned here, both against the REAL clipvoice.py on a throwaway pty (whisper is
kept out of the way with a bogus model path, the mic with a marker `sleep`
capture command):

- the terminal tears the pty down WITHOUT a signal -> the instant-ERR
  detector in the getch loop must exit;
- SIGHUP (a polite terminal close, or plain kill) -> the handler must exit.

Either way the capture child must be gone afterwards — no strays.
"""

import fcntl
import os
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _spawn(marker):
    master, slave = os.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ,
                b"\x18\x00\x50\x00\x00\x00\x00\x00")  # 24x80
    env = dict(os.environ, TERM="xterm-256color",
               CLIPVOICE_CAPTURE_CMD=f"exec sleep {marker}",
               CLIPVOICE_MODEL="/nonexistent-model-dir")
    p = subprocess.Popen([sys.executable, "clipvoice.py"], cwd=str(ROOT),
                         env=env, stdin=slave, stdout=slave, stderr=slave,
                         preexec_fn=os.setsid)
    os.close(slave)
    return p, master


def _wait_first_draw(master, deadline=15.0):
    os.set_blocking(master, False)
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            if os.read(master, 65536):
                return
        except BlockingIOError:
            time.sleep(0.05)
        except OSError:
            return
    raise AssertionError("clipvoice never drew its TUI")


def _expect_exit(p, marker, why):
    try:
        p.wait(timeout=6)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        p.wait()
        subprocess.run(["pkill", "-9", "-f", f"sleep {marker}"])
        raise AssertionError(f"clipvoice survived {why}")
    time.sleep(0.5)
    strays = subprocess.run(["pgrep", "-af", f"sleep {marker}"],
                            capture_output=True, text=True).stdout.strip()
    if strays:
        subprocess.run(["pkill", "-9", "-f", f"sleep {marker}"])
        raise AssertionError(f"capture stray after {why}: {strays}")


def test_pty_teardown_without_signal_exits():
    p, master = _spawn(marker="86397")
    _wait_first_draw(master)
    os.close(master)  # the terminal window closes; no SIGHUP is delivered
    _expect_exit(p, "86397", "pty teardown (no signal)")


def test_sighup_exits_and_kills_capture():
    p, master = _spawn(marker="86398")
    _wait_first_draw(master)
    os.kill(p.pid, signal.SIGHUP)
    _expect_exit(p, "86398", "SIGHUP")
    os.close(master)
