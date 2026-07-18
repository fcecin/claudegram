import asyncio
import types

import bot
from tests.fakes import FakeApp, FakeBot, make_fake_session, stream_text, result_msg


def _clear_outbox():
    bot.MEDIA_OUTBOX.mkdir(parents=True, exist_ok=True)
    for f in bot.MEDIA_OUTBOX.iterdir():
        try:
            f.unlink()
        except OSError:
            pass


async def test_media_outbox_sends_image_and_cleans_up():
    saved = bot.ALLOWED_USER_IDS
    bot.ALLOWED_USER_IDS = [123]
    _clear_outbox()
    img = bot.MEDIA_OUTBOX / "t-1.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake-image-bytes")
    (bot.MEDIA_OUTBOX / "t-1.caption").write_text("a drawing")
    app = FakeApp()
    try:
        task = asyncio.create_task(bot.media_outbox_loop(app))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert "a drawing" in app.bot.photos
        assert not img.exists()
        assert not (bot.MEDIA_OUTBOX / "t-1.caption").exists()
    finally:
        bot.ALLOWED_USER_IDS = saved
        _clear_outbox()


def test_nyx_is_a_mute_image_bot():
    home = bot.bot_home("nyx")
    assert home is not None
    assert (home / "tools" / "send").is_file()
    assert "tools/send" in (home / "main.md").read_text()
    cfg = bot.bot_config("nyx")
    assert cfg["model"] == "opus" and cfg["effort"] == "max" and cfg["voiceback"] is False


async def test_voiceback_opt_out_config():
    # a bot with config voiceback:false never voicebacks, even when voiceback is requested.
    orig = bot.synthesize_voice
    called = []
    bot.synthesize_voice = lambda text, voice=None: called.append(text)
    try:
        fb = FakeBot()
        sess = make_fake_session("nyx", script=[stream_text("here you go"), result_msg()])
        assert sess.config.get("voiceback") is False
        await bot.dispatch_to_claude(types.SimpleNamespace(bot=fb), sess, 1, None,
                                     "draw a cat", "text", voiceback=True)
        assert called == [], "voiceback:false bot must not synthesize"
        assert fb.voices == 0
    finally:
        bot.synthesize_voice = orig


async def test_oversized_image_document_rejected_up_front():
    # An image sent AS A FILE can exceed Telegram's 20 MB bot-download cap. It must be
    # rejected with guidance BEFORE get_file (which would fail with a generic error) —
    # the same pre-check handle_document and handle_audio already do.
    replies = []

    async def reply_text(text, **kw):
        replies.append(text)

    doc = types.SimpleNamespace(mime_type="image/png", file_size=bot.TG_BOT_DL_LIMIT + 1,
                                file_id="f-big", file_name="big.png")
    msg = types.SimpleNamespace(photo=[], document=doc, caption=None,
                                from_user=types.SimpleNamespace(id=123, full_name="T"),
                                chat_id=1, message_id=9, reply_text=reply_text)
    upd = types.SimpleNamespace(message=msg,
                                effective_user=types.SimpleNamespace(id=123, full_name="T"))
    ctx = types.SimpleNamespace(bot=FakeBot())   # FakeBot has no get_file: a download attempt = loud failure
    saved = bot.ALLOWED_USER_IDS
    bot.ALLOWED_USER_IDS = [123]
    try:
        await bot.handle_photo(upd, ctx)
    finally:
        bot.ALLOWED_USER_IDS = saved
    assert replies and "MB" in replies[0], replies    # clear size guidance, not a generic error
    assert bot.registry.current().pending == []       # and nothing was enqueued for Claude
