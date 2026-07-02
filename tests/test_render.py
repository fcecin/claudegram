"""Rendering: the color-bubble badge lands on every streamed message, the [[END]], and
alerts — and with no badge (single session) output is byte-identical to the original."""

import bot
from tests.fakes import FakeBot


async def test_streamer_prefixes_every_message_and_end():
    fb = FakeBot()
    ps = bot.ParagraphStreamer(fb, 1, None, prefix="🟢 gil · ")
    await ps.feed("hello world")
    await ps.finish()
    assert fb.sent[0].startswith("🟢 gil · ")
    assert fb.sent[-1] == "🟢 gil · [[END]]"


async def test_streamer_no_prefix_is_unchanged():
    fb = FakeBot()
    ps = bot.ParagraphStreamer(fb, 1, None, prefix="")
    await ps.feed("plain answer")
    await ps.finish()
    assert fb.sent[0] == "plain answer"
    assert fb.sent[-1] == "[[END]]"


async def test_alert_is_badged():
    fb = FakeBot()
    r = bot.SegmentRenderer(fb, 1, None, "🟢 gil · hdr", badge="🟢 gil · ")
    await r.alert("boom")
    assert fb.sent == ["🟢 gil · boom"]


async def test_alert_without_badge_unchanged():
    fb = FakeBot()
    r = bot.SegmentRenderer(fb, 1, None, "hdr", badge="")
    await r.alert("boom")
    assert fb.sent == ["boom"]
