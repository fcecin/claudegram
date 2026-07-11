"""cg-send + the preamble that teaches it: any bot can deliver a file (image, PDF,
any document) to the owner's phone by dropping it into media-outbox/, which
media_outbox_loop relays over Telegram (photo, else document)."""

import contextlib
import pathlib
import shutil
import subprocess
import tempfile

import bot
from tests.fakes import FakeBot


@contextlib.contextmanager
def _outbox(td):
    """Repoint bot.MEDIA_OUTBOX at a temp dir for one test."""
    old = bot.MEDIA_OUTBOX
    bot.MEDIA_OUTBOX = pathlib.Path(td)
    try:
        yield bot.MEDIA_OUTBOX
    finally:
        bot.MEDIA_OUTBOX = old


def _run_cg_send(root: pathlib.Path, *args: str):
    """Run a copy of cg-send from `root`, so its outbox lands in root/media-outbox."""
    script = root / "cg-send"
    if not script.exists():
        shutil.copy2(bot.HERE / "cg-send", script)
    return subprocess.run([str(script), *args], capture_output=True, text=True)


def test_preamble_teaches_cg_send():
    p = bot.build_prompt("hi there", "text")
    assert "cg-send" in p


def test_cg_send_drops_document_with_paired_caption():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        doc = root / "report.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        r = _run_cg_send(root, str(doc), "the quarterly report")
        assert r.returncode == 0, r.stderr

        files = sorted((root / "media-outbox").iterdir())
        media = [f for f in files if f.suffix == ".pdf"]
        caps = [f for f in files if f.suffix == ".caption"]
        assert len(media) == 1 and len(caps) == 1, files
        assert media[0].read_bytes() == b"%PDF-1.4 fake"
        assert caps[0].read_text() == "the quarterly report"
        # media_outbox_loop pairs the caption via with_suffix(".caption")
        assert media[0].with_suffix(".caption") == caps[0]
        # the Telegram document keeps a readable filename
        assert media[0].name.endswith("-report.pdf")
        # no leftover .tmp staging file
        assert not [f for f in files if f.suffix == ".tmp"]


def test_cg_send_without_caption_and_without_extension():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        doc = root / "README"
        doc.write_text("plain file, no extension")
        r = _run_cg_send(root, str(doc))
        assert r.returncode == 0, r.stderr

        files = sorted((root / "media-outbox").iterdir())
        assert len(files) == 1, files  # media only — no caption sidecar
        assert files[0].name.endswith("-README")
        # with_suffix(".caption") on the extensionless media must not explode the pairing
        assert files[0].with_suffix(".caption").name.endswith("-README.caption")


def test_cg_send_missing_file_fails_loudly():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        r = _run_cg_send(root, str(root / "nope.pdf"))
        assert r.returncode != 0
        assert "no such file" in r.stderr


async def test_outbox_routes_by_type_up_front():
    """A PDF must go out as a DOCUMENT (sendPhoto would rasterize page 1, not fail);
    an image goes as a photo. Captions pair; the outbox is consumed."""
    with tempfile.TemporaryDirectory() as td, _outbox(td) as outbox:
        (outbox / "1-report.pdf").write_bytes(b"%PDF")
        (outbox / "1-report.caption").write_text("the report")
        (outbox / "2-pic.png").write_bytes(b"\x89PNG")
        fb = FakeBot()
        assert await bot._drain_media_outbox(fb, 1) == 2
        assert fb.documents == ["the report"]  # pdf → document, caption attached
        assert fb.photos == [None]             # png → photo, no caption
        assert list(outbox.iterdir()) == []    # media + caption consumed


async def test_failed_document_send_is_never_retried_as_photo():
    """The mangler: a transient document failure retried via sendPhoto would deliver
    a rasterized page 1 pretending to be the file. Fail loudly instead."""
    class DocumentsFail(FakeBot):
        async def send_document(self, chat_id, document=None, caption=None, **kw):
            raise RuntimeError("boom")

    with tempfile.TemporaryDirectory() as td, _outbox(td) as outbox:
        (outbox / "1-report.pdf").write_bytes(b"%PDF")
        fb = DocumentsFail()
        assert await bot._drain_media_outbox(fb, 1) == 0
        assert fb.photos == []


async def test_failed_photo_send_falls_back_to_document():
    """An image Telegram rejects as a photo (huge/odd) still arrives — as a file."""
    class PhotosFail(FakeBot):
        async def send_photo(self, chat_id, photo=None, caption=None, **kw):
            raise RuntimeError("PHOTO_INVALID_DIMENSIONS")

    with tempfile.TemporaryDirectory() as td, _outbox(td) as outbox:
        (outbox / "1-huge.png").write_bytes(b"\x89PNG")
        fb = PhotosFail()
        assert await bot._drain_media_outbox(fb, 1) == 1
        assert fb.documents == [None]
