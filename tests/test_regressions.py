"""Regression tests for bugs fixed on 2026-07-01 — pinned forever:

  1. Voiceback spoke every reply in English (gTTS default) regardless of the text's
     language. Fix: detect_tts_lang() -> gTTS lang= per block.
  2. NO MORE WORK was never detected while `bot voice on`, because voiceback wraps the
     reply in VOICESTART/VOICEEND so the answer no longer started with the marker, and the
     detection lived AFTER the voiceback early-return. Fix: detect before the branch, strip
     the markers first.
"""

import bot
from tests.fakes import make_fake_session


# --- bug 1: voiceback language --------------------------------------------------
def test_voiceback_detects_portuguese():
    assert bot.detect_tts_lang("Olá, terminei a tarefa e está tudo funcionando.") == "pt"


def test_voiceback_detects_english_and_spanish():
    assert bot.detect_tts_lang("Hello, everything is done and the tests pass.") == "en"
    assert bot.detect_tts_lang("Hola, he terminado y todo funciona bien.") == "es"


def test_voiceback_falls_back_when_undetectable():
    # empty/garbage -> the default (never an exception, never wrong-crash)
    assert bot.detect_tts_lang("", default="en") == "en"


# --- bug 2: NO MORE WORK detection ----------------------------------------------
def _renderer(voiceback, answer):
    sess = make_fake_session("claude")
    r = bot.SegmentRenderer(bot_fake(), 1, None, "hdr", voiceback=voiceback,
                            session=sess, controller=sess.controller)
    r.answer_buf = [answer]
    r.result = {"text": "", "is_error": False, "subtype": "success", "turns": 1, "secs": 1.0}
    return sess, r


def bot_fake():
    from tests.fakes import FakeBot
    return FakeBot()


async def test_no_more_work_detected_plain():
    sess, r = _renderer(False, "NO MORE WORK — I'm out of tasks.")
    await r.finalize()
    assert sess.no_more_work is True


async def test_no_more_work_detected_under_voiceback():
    # Under voiceback the answer is collected (not streamed); NO MORE WORK must still be
    # detected before the voiceback branch. (Markers were removed — the whole reply is spoken.)
    orig = bot.synthesize_voice
    bot.synthesize_voice = lambda text, voice=None: None   # no TTS in tests
    try:
        sess, r = _renderer(True, "NO MORE WORK — I'm out of tasks.")
        await r.finalize()
        assert sess.no_more_work is True
    finally:
        bot.synthesize_voice = orig


async def test_normal_reply_does_not_declare_done():
    sess, r = _renderer(False, "Here is the answer you asked for.")
    await r.finalize()
    assert sess.no_more_work is False


async def test_no_more_work_detected_mid_reply():
    # Detected ANYWHERE now, not just at the start — bots routinely bury it mid-paragraph.
    sess, r = _renderer(False, "Deployed and verified on cg2/cg3. NO MORE WORK — standing down.")
    await r.finalize()
    assert sess.no_more_work is True


async def test_no_more_work_is_case_sensitive():
    # The nudge demands the exact UPPERCASE words. Ordinary prose that merely mentions
    # "no more work" (the old .upper() scan tripped on this) must NOT silence the nudger.
    sess, r = _renderer(False, "There is no more work needed on the parser; it's solid.")
    await r.finalize()
    assert sess.no_more_work is False
