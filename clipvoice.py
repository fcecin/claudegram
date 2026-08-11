#!/usr/bin/env python3
"""clipvoice — open-mic dictation straight into the Linux clipboard.

Fullscreen terminal TUI. While it runs it listens to the default microphone,
transcribes each utterance locally (faster-whisper, the same engine claudegram
uses for voice notes), and keeps the WHOLE transcript in the clipboard at all
times — both the CLIPBOARD selection (Ctrl+Shift+V in a terminal) and the
PRIMARY selection (middle mouse button). What is on screen is exactly what a
paste will produce.

Keys:
    Esc     wipe the transcript AND the clipboard, start fresh
    Ctrl+C  quit; the terminal comes back and the clipboard keeps the text

Config (env vars, or the matching --flags, flags win):
    CLIPVOICE_MODEL        whisper model size (default: small)
    CLIPVOICE_LANGUAGE     force a language code like en/pt (default: autodetect)
    CLIPVOICE_DEVICE       cpu | cuda (default: cpu)
    CLIPVOICE_COMPUTE     ctranslate2 compute type (default: int8 on cpu)
    CLIPVOICE_CAPTURE_CMD  shell command that writes raw s16le/16kHz/mono PCM
                           to stdout (default: parecord from the default mic);
                           also how the tests feed canned audio through the
                           whole pipeline.

Run via ./run-clipvoice.sh (shares claudegram's .venv). --headless skips the
TUI and prints each utterance as a line (for tests, or dictating over ssh).
"""

import argparse
import collections
import locale
import os
import queue
import shutil
import subprocess
import sys
import textwrap
import threading
import time

SR = 16000
FRAME = 480          # samples per analysis frame (30 ms)
PREROLL_FRAMES = 12  # 360 ms kept from before speech triggers
TRIGGER_FRAMES = 2   # consecutive loud frames that start an utterance
HANG_FRAMES = 24     # 720 ms of quiet that ends an utterance
MIN_SPEECH_FRAMES = 6  # utterances with less actual speech than this are noise
MAX_SEG_SECONDS = 25   # force a cut so a monologue still streams out


class Shared:
    """Everything the reader/worker threads and the UI exchange, under one lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.parts = []          # transcribed utterances, in order
        self.gen = 0             # bumped by Esc; stale segments are discarded
        self.level = 0.0         # latest mic RMS, for the meter
        self.in_speech = False
        self.model_ready = False
        self.transcribing = False
        self.capture_eof = False
        self.error = None

    def text(self):
        with self.lock:
            return " ".join(self.parts)


# ---------------------------------------------------------------- clipboard

WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))


def _pipe_to(cmd, data):
    try:
        subprocess.run(cmd, input=data, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def clipboard_set(text):
    data = text.encode()
    if WAYLAND and shutil.which("wl-copy"):
        _pipe_to(["wl-copy"], data)
        _pipe_to(["wl-copy", "--primary"], data)
    elif shutil.which("xclip"):
        _pipe_to(["xclip", "-selection", "clipboard"], data)
        _pipe_to(["xclip", "-selection", "primary"], data)


def clipboard_clear():
    if WAYLAND and shutil.which("wl-copy"):
        _pipe_to(["wl-copy", "--clear"], b"")
        _pipe_to(["wl-copy", "--primary", "--clear"], b"")
    else:
        clipboard_set("")


# ---------------------------------------------------------------- pipeline


class Pipeline:
    """capture process -> reader thread (VAD segmentation) -> worker thread
    (whisper decode -> transcript -> clipboard)."""

    def __init__(self, args, shared):
        self.args = args
        self.shared = shared
        self.q = queue.Queue()
        self.proc = None
        self.stop_evt = threading.Event()
        self.threads = []

    def start(self):
        capture_env = os.environ.get("CLIPVOICE_CAPTURE_CMD", "").strip()
        try:
            if capture_env:
                self.proc = subprocess.Popen(
                    capture_env, shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            else:
                self.proc = subprocess.Popen(
                    ["parecord", "--rate=16000", "--channels=1",
                     "--format=s16le", "--raw"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            self.shared.error = f"cannot start audio capture: {e}"
            return
        for fn in (self._reader, self._worker):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_evt.set()
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.q.put(None)
        for t in self.threads:
            t.join(timeout=1.5)

    # -- reader: raw PCM -> utterance segments ------------------------------

    def _reader(self):
        import numpy as np

        sh = self.shared
        out = self.proc.stdout
        preroll = collections.deque(maxlen=PREROLL_FRAMES)
        seg, in_speech, silence_run, trigger_run, speech_frames = [], False, 0, 0, 0
        floor_samples, floor = [], 0.0
        thresh = 260.0
        max_frames = MAX_SEG_SECONDS * SR // FRAME
        pending = b""

        def emit():
            nonlocal seg, in_speech, silence_run, speech_frames
            if seg and speech_frames >= MIN_SPEECH_FRAMES:
                audio = np.concatenate(seg).astype(np.float32) / 32768.0
                with sh.lock:
                    gen = sh.gen
                self.q.put((gen, audio))
            seg, in_speech, silence_run, speech_frames = [], False, 0, 0

        while not self.stop_evt.is_set():
            chunk = out.read(FRAME * 2 - len(pending))
            if not chunk:
                break
            pending += chunk
            if len(pending) < FRAME * 2:
                continue
            frame = np.frombuffer(pending, dtype=np.int16)
            pending = b""
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))

            if len(floor_samples) < 20:  # calibrate on the first ~0.6 s
                floor_samples.append(rms)
                if len(floor_samples) == 20:
                    floor = max(float(np.median(floor_samples)), 40.0)
                    thresh = max(3.0 * floor, 260.0)
                continue

            speech = rms > thresh
            with sh.lock:
                sh.level, sh.in_speech = rms, in_speech or speech

            if not in_speech:
                preroll.append(frame)
                trigger_run = trigger_run + 1 if speech else 0
                if trigger_run >= TRIGGER_FRAMES:
                    in_speech, trigger_run = True, 0
                    seg, silence_run, speech_frames = list(preroll), 0, TRIGGER_FRAMES
                    preroll.clear()
                elif not speech:  # track the ambient noise floor while idle
                    floor = 0.97 * floor + 0.03 * rms
                    thresh = max(3.0 * floor, 260.0)
            else:
                seg.append(frame)
                if speech:
                    silence_run, speech_frames = 0, speech_frames + 1
                else:
                    silence_run += 1
                if silence_run >= HANG_FRAMES or len(seg) >= max_frames:
                    emit()

        emit()  # whatever was in flight when the stream ended
        with sh.lock:
            sh.capture_eof, sh.level, sh.in_speech = True, 0.0, False
        if self.proc.returncode not in (None, 0) and not self.stop_evt.is_set():
            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
            if err:
                sh.error = f"audio capture died: {err.splitlines()[-1]}"

    # -- worker: segments -> text -> clipboard ------------------------------

    def _worker(self):
        sh = self.shared
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(self.args.model, device=self.args.device,
                                 compute_type=self.args.compute)
        except Exception as e:
            sh.error = f"whisper model failed to load: {e}"
            return
        with sh.lock:
            sh.model_ready = True

        while not self.stop_evt.is_set():
            item = self.q.get()
            if item is None:
                break
            gen, audio = item
            with sh.lock:
                sh.transcribing = True
            try:
                # condition_on_previous_text=False: the documented cure for
                # whisper's infinite-repetition decode loop (see
                # transcribe_worker.py, which this mirrors).
                segments, _info = model.transcribe(
                    audio, language=self.args.lang, beam_size=5,
                    vad_filter=True, condition_on_previous_text=False)
                text = "".join(s.text for s in segments).strip()
            except Exception as e:
                sh.error = f"transcription failed: {e}"
                text = ""
            with sh.lock:
                sh.transcribing = False
                if text and gen == sh.gen:
                    sh.parts.append(text)
                    full = " ".join(sh.parts)
                else:
                    full = None
            if full is not None:
                clipboard_set(full)

    def wipe(self):
        """Esc: forget everything, including whatever is still in the queue."""
        with self.shared.lock:
            self.shared.gen += 1
            self.shared.parts.clear()
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        clipboard_clear()

    def idle(self):
        with self.shared.lock:
            busy = self.shared.transcribing
        return self.shared.capture_eof and self.q.empty() and not busy


# ---------------------------------------------------------------- TUI

SPINNER = "|/-\\"


def draw(stdscr, shared, args, tick):
    import curses

    h, w = stdscr.getmaxyx()
    stdscr.erase()
    with shared.lock:
        level, in_speech = shared.level, shared.in_speech
        ready, busy = shared.model_ready, shared.transcribing
        eof, error = shared.capture_eof, shared.error
        text = " ".join(shared.parts)

    def put(y, x, s, attr=0):
        if 0 <= y < h:
            try:
                stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)
            except curses.error:
                pass

    title = f" clipvoice — everything below is in your clipboard "
    info = f" model:{args.model} lang:{args.lang or 'auto'} "
    put(0, 0, title.ljust(w), curses.A_REVERSE)
    put(0, max(0, w - len(info) - 1), info, curses.A_REVERSE)

    bar_w = 20
    fill = min(bar_w, int((level / 3000.0) * bar_w))
    meter = "[" + "=" * fill + " " * (bar_w - fill) + "]"
    if error:
        status = f"ERROR: {error}"
        attr = curses.A_BOLD
    elif not ready:
        status = f"{meter} loading whisper '{args.model}'… (first run downloads it) {SPINNER[tick % 4]}"
        attr = curses.A_DIM
    elif eof:
        status = "mic stream ended — Ctrl+C to leave (clipboard keeps the text)"
        attr = curses.A_BOLD
    else:
        state = "hearing you" if in_speech else "listening"
        status = f"{meter} {state}"
        if busy:
            status += f"   transcribing {SPINNER[tick % 4]}"
        attr = curses.A_BOLD if in_speech else 0
    put(1, 1, status, attr)
    put(2, 0, "─" * (w - 1), curses.A_DIM)

    body_top, body_bot = 3, h - 2
    lines = []
    for para in text.split("\n") if text else []:
        lines.extend(textwrap.wrap(para, max(10, w - 4)) or [""])
    shown = lines[-(body_bot - body_top):]
    for i, ln in enumerate(shown):
        put(body_top + i, 2, ln)
    if not text and not error:
        put(body_top, 2, "(say something — each pause becomes a paste-ready "
                         "sentence here)", curses.A_DIM)

    footer = " Esc wipe transcript+clipboard · Ctrl+C quit (text stays in clipboard) "
    counter = f" clipboard: {len(text)} chars "
    put(h - 1, 0, footer.ljust(w), curses.A_REVERSE)
    put(h - 1, max(0, w - len(counter) - 1), counter, curses.A_REVERSE)
    stdscr.refresh()


def main_tui(args, shared, pipe):
    import curses

    # curses renders the TUI's non-ASCII glyphs correctly only under the
    # user's locale (UTF-8 everywhere that matters), not the default "C".
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")

    def run(stdscr):
        curses.curs_set(0)
        curses.raw()
        stdscr.keypad(True)
        stdscr.timeout(80)
        tick = 0
        while True:
            draw(stdscr, shared, args, tick)
            tick += 1
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                return
            if ch == 3:            # Ctrl+C (raw mode)
                return
            if ch == 27:           # Esc
                pipe.wipe()

    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass


def main_headless(args, shared, pipe):
    printed = 0
    try:
        while not (pipe.idle() and printed == len(shared.parts)):
            with shared.lock:
                new = shared.parts[printed:]
            for part in new:
                print(part, flush=True)
                printed += 1
            if shared.error:
                print(f"clipvoice: {shared.error}", file=sys.stderr)
                return 1
            time.sleep(0.15)
    except KeyboardInterrupt:
        pass
    n = len(shared.text())
    print(f"clipvoice: done, clipboard holds {n} chars", file=sys.stderr)
    return 0


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=env("CLIPVOICE_MODEL", "small"))
    ap.add_argument("--lang", default=env("CLIPVOICE_LANGUAGE", "") or None)
    ap.add_argument("--device", default=env("CLIPVOICE_DEVICE", "cpu"))
    ap.add_argument("--compute", default=env("CLIPVOICE_COMPUTE",
                                             "int8"))
    ap.add_argument("--headless", action="store_true",
                    help="no TUI; print each utterance, exit when audio ends")
    args = ap.parse_args()

    shared = Shared()
    pipe = Pipeline(args, shared)
    pipe.start()
    try:
        if args.headless:
            return main_headless(args, shared, pipe) or 0
        main_tui(args, shared, pipe)
        return 0
    finally:
        pipe.stop()


if __name__ == "__main__":
    sys.exit(main())
