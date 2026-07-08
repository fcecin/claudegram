import pathlib
import tempfile

import bot


def test_wake_drop_injects_turn_into_current_bot():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cg-wake-"))
    old = bot.WAKE_INBOX
    bot.WAKE_INBOX = tmp
    try:
        (tmp / "1.msg").write_text("tick 2026-07-08 16:00: anything to do?", encoding="utf-8")
        (tmp / ".partial.tmp").write_text("half-written drop", encoding="utf-8")  # ignored
        (tmp / "note.txt").write_text("not a .msg", encoding="utf-8")             # ignored
        n = bot._drain_wake_inbox(chat=123)
        assert n == 1
        cur = bot.registry.current()
        assert len(cur.pending) == 1
        m = cur.pending[0]
        assert m["text"].startswith("tick 2026-07-08")
        assert m["source"] == "wake"
        assert m["chat_id"] == 123
        assert m["reply_to"] is None
        # consumed only the finished .msg; left the temp + non-msg files
        assert not (tmp / "1.msg").exists()
        assert (tmp / ".partial.tmp").exists()
        assert (tmp / "note.txt").exists()
    finally:
        bot.WAKE_INBOX = old


def test_wake_empty_inbox_is_noop():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cg-wake-"))
    old = bot.WAKE_INBOX
    bot.WAKE_INBOX = tmp
    try:
        assert bot._drain_wake_inbox(chat=123) == 0
        assert len(bot.registry.current().pending) == 0
    finally:
        bot.WAKE_INBOX = old


def test_wake_multiple_drops_injected_in_name_order():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cg-wake-"))
    old = bot.WAKE_INBOX
    bot.WAKE_INBOX = tmp
    try:
        (tmp / "1000.msg").write_text("first", encoding="utf-8")
        (tmp / "2000.msg").write_text("second", encoding="utf-8")
        assert bot._drain_wake_inbox(chat=7) == 2
        assert [m["text"] for m in bot.registry.current().pending] == ["first", "second"]
    finally:
        bot.WAKE_INBOX = old
