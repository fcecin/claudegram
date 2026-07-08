import os
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _fake_crontab(tmp: pathlib.Path) -> pathlib.Path:
    """A crontab(1) stand-in backed by a file: `-l` prints it, `-` reads stdin into it. Lets
    install-cron.sh run with zero real side effects (never touches the user's real crontab)."""
    p = tmp / "fakecrontab"
    p.write_text(
        "#!/usr/bin/env bash\n"
        'F="$FAKE_CRON_FILE"\n'
        'if [ "${1:-}" = "-l" ]; then cat "$F" 2>/dev/null; exit 0; fi\n'
        'if [ "${1:-}" = "-" ]; then cat > "$F"; exit 0; fi\n'
        "exit 0\n")
    p.chmod(0o755)
    return p


def _run(args, env):
    return subprocess.run(args, env=env, cwd=str(ROOT), capture_output=True, text=True)


def test_wake_cron_install_idempotent_and_isolated():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cg-cron-"))
    try:
        fake = _fake_crontab(tmp)
        cronfile = tmp / "crontab.txt"
        # Pre-existing entries that MUST survive our install/remove untouched:
        #  - a different install ('other')
        #  - a slug for which ours ('testinstall') is a PREFIX ('testinstall-two') — guards the
        #    end-of-line anchor against substring collisions.
        seed = ("0 0,3,6,9,12,15,18,21 * * * cd '/o' && ./cg-wake # claudegram-wake:other\n"
                "0 0,3,6,9,12,15,18,21 * * * cd '/t2' && ./cg-wake # claudegram-wake:testinstall-two\n")
        cronfile.write_text(seed, encoding="utf-8")
        env = dict(os.environ, CRONTAB_BIN=str(fake), FAKE_CRON_FILE=str(cronfile),
                   WAKE_CRON_SLUG="testinstall")
        script = str(ROOT / "install-cron.sh")

        r = _run([script, str(ROOT)], env)
        assert r.returncode == 0, r.stderr
        body = cronfile.read_text()
        assert "# claudegram-wake:other" in body
        assert "# claudegram-wake:testinstall-two" in body
        assert "0 0,3,6,9,12,15,18,21 * * *" in body            # correct 3-hour schedule
        ours = sum(1 for ln in body.splitlines() if ln.endswith("# claudegram-wake:testinstall"))
        assert ours == 1                                        # added exactly once

        # idempotent: reinstall => still exactly one of ours; neighbours preserved
        r = _run([script, str(ROOT)], env)
        assert r.returncode == 0, r.stderr
        body = cronfile.read_text()
        ours = sum(1 for ln in body.splitlines() if ln.endswith("# claudegram-wake:testinstall"))
        assert ours == 1
        assert "# claudegram-wake:other" in body
        assert "# claudegram-wake:testinstall-two" in body

        # remove: ours gone; neighbours (incl. the prefix-collision slug) untouched
        r = _run([script, str(ROOT), "--remove"], env)
        assert r.returncode == 0, r.stderr
        body = cronfile.read_text()
        assert not any(ln.endswith("# claudegram-wake:testinstall") for ln in body.splitlines())
        assert "# claudegram-wake:other" in body
        assert "# claudegram-wake:testinstall-two" in body
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
