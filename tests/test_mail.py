"""cg-mail regressions — offline: a sandboxed copy of the script runs against a FAKE curl
on PATH that captures the JSON payload instead of talking to Resend. Pins the payload
shape (recipient list, subject, body, base64 attachments, resend_from sender) across the
refactor that moved the body off the python builder's argv (argv is world-readable in
`ps`; the body now arrives via stdin)."""

import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import bot

_FAKE_CURL = """#!/usr/bin/env bash
# fake curl: save the --data-binary payload file, swallow the -K - config from stdin,
# answer like Resend (JSON id + HTTP 201 status line, matching -w '\\n%{http_code}').
payload=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--data-binary" ]; then payload="${a#@}"; fi
  prev="$a"
done
cat >/dev/null
cp "$payload" "%CAPTURE%"
printf '{"id":"fake-123"}\\n201'
"""


def _sandbox():
    tmp = Path(tempfile.mkdtemp(prefix="cg-mail-test-"))
    shutil.copy(bot.HERE / "cg-mail", tmp / "cg-mail")
    os.chmod(tmp / "cg-mail", os.stat(tmp / "cg-mail").st_mode | stat.S_IXUSR)
    (tmp / "resend.key").write_text("re_test_key\n", encoding="utf-8")
    (tmp / "instance.json").write_text(
        json.dumps({"resend_from": "me@example.com"}), encoding="utf-8")
    bindir = tmp / "bin"
    bindir.mkdir()
    capture = tmp / "captured.json"
    curl = bindir / "curl"
    curl.write_text(_FAKE_CURL.replace("%CAPTURE%", str(capture)), encoding="utf-8")
    os.chmod(curl, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    return tmp, env, capture


def test_cg_mail_payload_recipients_body_and_attachment():
    tmp, env, capture = _sandbox()
    try:
        att = tmp / "att.bin"
        att.write_bytes(b"\x00\x01binary-bytes")
        r = subprocess.run(
            [str(tmp / "cg-mail"), "-a", str(att),
             "a@x.com, b@y.com", "Subject here", "hello", "body"],
            env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (r.stdout, r.stderr)
        p = json.loads(capture.read_text(encoding="utf-8"))
        assert p["from"] == "me@example.com"                  # instance.json resend_from
        assert p["to"] == ["a@x.com", "b@y.com"]              # comma list split + trimmed
        assert p["subject"] == "Subject here"
        assert p["text"] == "hello body"                      # args joined into the body
        assert p["attachments"][0]["filename"] == "att.bin"
        assert base64.b64decode(p["attachments"][0]["content"]) == b"\x00\x01binary-bytes"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cg_mail_body_via_stdin_and_not_via_python_argv():
    tmp, env, capture = _sandbox()
    try:
        # stdin-body form still works…
        r = subprocess.run([str(tmp / "cg-mail"), "who@x.com", "S2"],
                           input="stdin body here", env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (r.stdout, r.stderr)
        p = json.loads(capture.read_text(encoding="utf-8"))
        assert p["text"] == "stdin body here"
        assert p["to"] == ["who@x.com"]
        # …and the script must PIPE the body to the payload builder, never pass it in argv
        # (argv is visible to every local process via `ps`).
        src = (bot.HERE / "cg-mail").read_text(encoding="utf-8")
        assert "printf '%s' \"$body\" | python3" in src
        assert '"$from" "$to" "$subject" "$body"' not in src
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
