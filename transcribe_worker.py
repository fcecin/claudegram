#!/usr/bin/env python3
"""Standalone whisper transcription worker.

Run as its OWN PROCESS by bot.py (``python transcribe_worker.py <audio_path>``), not
imported into it. Why a separate process: a whisper decode can stall or loop forever,
and **you cannot kill a Python thread** — only a process. So the bridge runs the decode
here and, if it blows past its watchdog budget, kills this process outright. Running as a
script (never imported by bot.py) also means spawning it has zero side effects on the
bridge: no Telegram, no Claude controller, no model loaded in the parent.

IPC is plain stdout, one line per record:
    PROGRESS <pct> <eta_secs>     real progress from seg.end / audio duration (eta -1 = n/a)
    RESULT   <json>               final transcript + diagnostics (exactly one, last)
    ERROR    <json-string>        a traceback, if the decode raised
faster-whisper's own logs are suppressed (no logging handler in this process), so stdout
carries only these records. Config is the same WHISPER_* env vars, inherited from bot.py.
"""

import json
import os
import sys
import time

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "large-v3").strip()
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu").strip()
COMPUTE_TYPE = os.environ.get(
    "WHISPER_COMPUTE_TYPE", "float32" if DEVICE == "cpu" else "float16"
).strip()
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "").strip() or None

PROGRESS_INTERVAL = 3.0  # min seconds between PROGRESS lines (also gated by segment yields)

_model = None


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    return _model


def transcribe(path: str, progress=None) -> dict:
    """Blocking transcription. Returns text + diagnostics, calling ``progress(pct, eta)``
    at most every PROGRESS_INTERVAL seconds with the REAL position (seg.end / duration).

    ``condition_on_previous_text=False`` is the documented cure for whisper's
    infinite-repetition decode loop (the decoder conditioning on its own hallucinated
    output and never terminating) — the exact failure mode the watchdog exists to catch.
    """
    model = _get_model()
    t0 = time.monotonic()
    segments, info = model.transcribe(
        path,
        language=LANGUAGE,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    duration = info.duration or 0.0
    parts = []
    last_report = t0
    prev_done = prev_elapsed = 0.0
    reports = 0
    for seg in segments:  # generator: consuming it is where the work happens
        parts.append(seg.text)
        if progress and duration > 0:
            now = time.monotonic()
            if now - last_report >= PROGRESS_INTERVAL:
                last_report = now
                done = min(max(seg.end / duration, 0.0), 1.0)
                elapsed = now - t0
                # ETA from the RECENT rate (between reports) — excludes the one-time
                # warmup that skews a cumulative estimate. -1 on the first report.
                eta = -1.0
                if reports >= 1:
                    d_done, d_elapsed = done - prev_done, elapsed - prev_elapsed
                    if d_done > 0 and d_elapsed > 0:
                        eta = (1.0 - done) / (d_done / d_elapsed)
                prev_done, prev_elapsed = done, elapsed
                reports += 1
                progress(done * 100.0, eta)
    return {
        "text": "".join(parts).strip(),
        "language": info.language,
        "language_probability": info.language_probability,
        "audio_seconds": info.duration,
        "elapsed_seconds": time.monotonic() - t0,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: transcribe_worker.py <audio_path>\n")
        sys.exit(2)

    def _progress(pct, eta):
        _emit(f"PROGRESS {pct:.1f} {eta:.1f}")

    try:
        result = transcribe(sys.argv[1], _progress)
    except Exception:
        import traceback

        _emit("ERROR " + json.dumps(traceback.format_exc()))
        sys.exit(1)
    _emit("RESULT " + json.dumps(result))
