import bot


def test_config_bot_pins_its_codec_at_spawn():
    for name in ("sno", "ily", "gol"):
        assert bot.session_compute(bot.Session(name)) == "int8", name          # config: fast
    for name in ("blu", "gil"):
        assert bot.session_compute(bot.Session(name)) == "int8_float32", name  # config: good
    assert bot.session_compute(bot.Session("ava")) == "float32"                # config: best


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
    c = bot.ClaudeController("/tmp/cg-effort-test", "/tmp/cg-effort-test.id")
    assert c.effort == "high"                            # code default at spawn
    assert await c.set_effort("low")
    assert c.effort == "low"                             # in-memory change
    # a fresh controller (== a restart) is back to the default, not "low"
    c2 = bot.ClaudeController("/tmp/cg-effort-test", "/tmp/cg-effort-test.id")
    assert c2.effort == "high"


# --- handle_audio: the received recording is kept as a reusable work piece --------
import asyncio as _asyncio      # noqa: E402
import json as _json            # noqa: E402
import pathlib as _pathlib      # noqa: E402
import shutil as _shutil        # noqa: E402
import tempfile as _tempfile    # noqa: E402
import types as _types          # noqa: E402

from tests.fakes import FakeBot as _FakeBot  # noqa: E402


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    """Stand-in for the transcribe_worker subprocess: streams canned stdout records."""
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.returncode = None
        self.pid = 4242

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _voice_update(fb, file_size=2048):
    async def reply_text(text, **kw):
        fb.sent.append(text)
    media = _types.SimpleNamespace(file_id="v1", file_size=file_size, duration=3)
    msg = _types.SimpleNamespace(
        voice=media, audio=None, video_note=None, video=None, caption=None,
        from_user=_types.SimpleNamespace(id=123, full_name="Claudia"),
        chat_id=1, message_id=7, reply_text=reply_text)
    upd = _types.SimpleNamespace(
        message=msg, effective_user=_types.SimpleNamespace(id=123, full_name="Claudia"))
    return upd, _types.SimpleNamespace(bot=fb)


async def test_voice_recording_is_preserved_as_a_work_piece():
    # A successfully-transcribed voice message must KEEP its original .oga in
    # work/incoming-audio (a reusable work piece), and hand Claude the saved path.
    saved_dir, saved_ids = bot.AUDIO_DIR, bot.ALLOWED_USER_IDS
    saved_cse = _asyncio.create_subprocess_exec
    tmpdir = _pathlib.Path(_tempfile.mkdtemp(prefix="cg-audio-test-"))
    bot.AUDIO_DIR = tmpdir
    bot.ALLOWED_USER_IDS = [123]
    result = {"text": "narracao de teste", "language": "pt",
              "audio_seconds": 3.0, "elapsed_seconds": 0.2}

    async def fake_cse(*a, **k):
        return _FakeProc([b"RESULT " + _json.dumps(result).encode() + b"\n"])
    _asyncio.create_subprocess_exec = fake_cse

    fb = _FakeBot()

    class _TgFile:
        async def download_to_drive(self, path):
            with open(path, "wb") as f:
                f.write(b"OggS-fake-voice-bytes")

    async def get_file(file_id):
        return _TgFile()
    fb.get_file = get_file

    upd, ctx = _voice_update(fb)
    try:
        await bot.handle_audio(upd, ctx)
        kept = list(tmpdir.glob("voice-*.oga"))
        assert len(kept) == 1, f"recording should be kept in work/, got {kept}"
        assert kept[0].read_bytes() == b"OggS-fake-voice-bytes"     # the real file, not empty
        pend = bot.registry.current().pending
        assert pend, "nothing enqueued for Claude"
        enq = pend[-1]["text"]
        assert "narracao de teste" in enq                          # the transcript, and...
        assert "saved at" in enq and str(kept[0]) in enq           # ...the audio's saved path
    finally:
        _asyncio.create_subprocess_exec = saved_cse
        bot.AUDIO_DIR, bot.ALLOWED_USER_IDS = saved_dir, saved_ids
        _shutil.rmtree(tmpdir, ignore_errors=True)


async def test_failed_audio_download_leaves_no_junk_file():
    # If the download fails, the empty placeholder file must be cleaned up (no litter in work/)
    # and nothing enqueued; the user gets clear size guidance.
    saved_dir, saved_ids = bot.AUDIO_DIR, bot.ALLOWED_USER_IDS
    tmpdir = _pathlib.Path(_tempfile.mkdtemp(prefix="cg-audio-test-"))
    bot.AUDIO_DIR = tmpdir
    bot.ALLOWED_USER_IDS = [123]
    fb = _FakeBot()

    async def get_file(file_id):
        raise Exception("Bad Request: File is too big")
    fb.get_file = get_file

    upd, ctx = _voice_update(fb)
    try:
        await bot.handle_audio(upd, ctx)
        assert list(tmpdir.glob("voice-*")) == []                  # junk placeholder removed
        assert bot.registry.current().pending == []                # nothing enqueued
        assert any("MB" in t for t in fb.edited), fb.edited        # user got size guidance
    finally:
        bot.AUDIO_DIR, bot.ALLOWED_USER_IDS = saved_dir, saved_ids
        _shutil.rmtree(tmpdir, ignore_errors=True)
