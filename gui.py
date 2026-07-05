#!/usr/bin/env python3
"""
claudegram tray app.

Runs in the system tray, supervises bot.py as a child process, and streams its
live log into a console window so you can see what the bot is doing. The bot
itself is unchanged — this is a supervisor + log viewer.

  - Tray icon: left-click toggles the console window.
  - Tray menu: Show console / Restart bot / Quit.
  - Closing the window hides it to the tray (the bot keeps running).
  - If the bot crashes, it is restarted automatically (with backoff).
  - Single-instance PER DIRECTORY: a second launch of the SAME install just focuses the
    first, but a copy in ANOTHER directory runs its own independent tray (its own token =
    its own Telegram bot). Named copies get a distinct title + colored tray badge so the
    windows are tell-apart-able; see instance_id.py and the optional `instance.txt`.

Launched at login via ~/.config/autostart/<install>.desktop (see install-autostart.sh).
"""

import sys
from pathlib import Path

import instance_id

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer, QElapsedTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QTextCursor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QSystemTrayIcon,
    QToolBar,
    QWidget,
)

HERE = Path(__file__).resolve().parent
PYTHON = str(HERE / ".venv" / "bin" / "python")
BOT = str(HERE / "bot.py")
LOG_FILE = HERE / "claudegram.log"   # persistent copy of the bot's output
BLOCK_FILE = HERE / "BLOCKED.flag"   # presence = bridge is locked (firewall trip)
SLEEP_FILE = HERE / "SLEEP.flag"     # presence = sleep mode (Telegram input paused)
INTRUSION_OFF_FILE = HERE / "INTRUSION_OFF.flag"  # presence = paranoid intrusion gate OFF (default ON)
REGRESSIONS_FILE = HERE / "HACKING_REGRESSIONS.md"  # false-positive list to append to
INSTANCE_JSON = HERE / "instance.json"  # declared per-install identity {name, color, glyph}
INSTANCE_FILE = HERE / "instance.txt"   # legacy identity fallback (name / #color / glyph lines)


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return None


# --- per-install identity ---------------------------------------------------------
# Run several copies of claudegram (each its OWN directory + token = its own Telegram bot),
# and each tray must be launchable and tell-apart-able. The single-instance key is keyed to THIS
# directory (not global), so copies never collide. A named copy gets a distinct title + tray
# badge; identity is DECLARED in instance.json (name/color/glyph), with instance.txt and finally
# the directory name as fallbacks. The canonical lone `claudegram` install is left as before.
APP_ID = instance_id.instance_key(str(HERE))   # single-instance key, unique per directory
_INST_JSON = _read_text(INSTANCE_JSON)
_INST_TXT = _read_text(INSTANCE_FILE) if _INST_JSON is None else None
_HAS_IDENTITY = _INST_JSON is not None or _INST_TXT is not None
LABEL, _INST_COLOR, _INST_GLYPH, IS_DEFAULT = instance_id.resolve(HERE.name, _INST_JSON, _INST_TXT)
TITLE = "claudegram" if IS_DEFAULT else f"claudegram · {LABEL}"
DESKTOP_NAME = instance_id.desktop_name(HERE.name, LABEL, _HAS_IDENTITY)

# Toolbar buttons get real chrome (border, hover highlight, pressed feedback) instead of
# flat text. Explicit colors so it looks deliberate under any system theme; blue accent
# matches the tray icon.
TOOLBAR_CSS = """
QToolBar { spacing: 6px; padding: 5px; }
QToolButton {
    background: #f0f1f3; color: #1f2937;
    border: 1px solid #c4c8cf; border-radius: 6px;
    padding: 6px 14px; font-weight: 600;
}
QToolButton:hover { background: #e3effb; border-color: #2b6cb0; }
QToolButton:pressed { background: #cfe0f4; border-color: #2b6cb0; }
"""
# Intrusion toggle: a physical switch — green & sunken when ON, grey & raised when OFF.
INTRUSION_ON_CSS = """
QPushButton {
    background: #2e9e4f; color: white;
    border: 3px inset #1c6b34; border-radius: 6px;
    padding: 6px 18px; font-weight: 700;
}
QPushButton:hover { background: #34b259; }
"""
INTRUSION_OFF_CSS = """
QPushButton {
    background: #e0a106; color: #3a2c00;
    border: 3px outset #b5820a; border-radius: 6px;
    padding: 6px 18px; font-weight: 700;
}
QPushButton:hover { background: #efb422; }
"""

MAX_FAST_FAILS = 6      # give up auto-restart after this many quick crashes
FAST_FAIL_SECS = 10_000  # an exit sooner than this (ms) counts as a "fast fail"


def make_icon() -> QIcon:
    """A microphone from the theme, or a drawn 'C' badge as fallback."""
    themed = QIcon.fromTheme("audio-input-microphone")
    if not themed.isNull():
        return themed
    return make_badge_icon(QColor("#2b6cb0"), "C")


def make_badge_icon(color: QColor, glyph: str) -> QIcon:
    """A filled circle in `color` with `glyph` (a letter or emoji) centered — the tray badge
    that tells one running install apart from another at a glance."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    f = QFont()
    f.setPointSize(30 if len(glyph) <= 1 else 22)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, glyph[:2])
    p.end()
    return QIcon(pm)


def instance_icon() -> QIcon:
    """The default microphone for the canonical install, or a per-install colored badge for a
    named copy (explicit color/glyph from instance.txt, else auto-derived from the label)."""
    if IS_DEFAULT:
        return make_icon()
    color = QColor(_INST_COLOR) if _INST_COLOR else QColor.fromHsv(*instance_id.accent_hsv(LABEL))
    return make_badge_icon(color, instance_id.badge_glyph(LABEL, _INST_GLYPH))


class Supervisor(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.icon = instance_icon()
        self._quitting = False
        self._fast_fails = 0
        self._uptime = QElapsedTimer()
        self.proc: QProcess | None = None
        self._log_fh = self._open_log()

        # --- console window ---------------------------------------------------
        self.setWindowTitle(TITLE)
        self.setWindowIcon(self.icon)
        self.resize(860, 500)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(8000)  # cap memory on long sessions
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(10)
        self.view.setFont(mono)
        self.setCentralWidget(self.view)

        tb = QToolBar()
        tb.setMovable(False)
        tb.setStyleSheet(TOOLBAR_CSS)
        self.addToolBar(tb)
        act_restart = QAction("Restart bot", self)
        act_restart.triggered.connect(self.restart_bot)
        tb.addAction(act_restart)
        self.act_unblock = QAction("🔓 UNBLOCK", self)
        self.act_unblock.triggered.connect(self.unblock)
        self.act_unblock.setVisible(False)
        tb.addAction(self.act_unblock)
        self.act_regress = QAction("🔓 Unlock & add regression", self)
        self.act_regress.triggered.connect(self.unblock_and_regress)
        self.act_regress.setVisible(False)
        tb.addAction(self.act_regress)
        self.act_wake = QAction("☀️ WAKE UP", self)
        self.act_wake.triggered.connect(self.wake)
        self.act_wake.setVisible(False)
        tb.addAction(self.act_wake)
        act_clear = QAction("Clear logs", self)
        act_clear.triggered.connect(self.clear_logs)
        tb.addAction(act_clear)
        act_hide = QAction("Hide to tray", self)
        act_hide.triggered.connect(self.hide)
        tb.addAction(act_hide)
        # Intrusion lock: a real checkable on/off switch, pushed to the far right and
        # visually distinct from the action buttons.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.intrusion_btn = QPushButton("🛡 Intrusion Lock")
        self.intrusion_btn.setCheckable(True)
        self.intrusion_btn.setCursor(Qt.PointingHandCursor)
        self.intrusion_btn.setToolTip(
            "ON: a message from any non-allowed Telegram user hard-locks the bridge "
            "(kills Claude + locks) and alerts you. Toggle here only — never remotely.")
        self.intrusion_btn.clicked.connect(self.toggle_intrusion)
        tb.addWidget(self.intrusion_btn)

        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("starting…")
        self.statusBar().addPermanentWidget(self.status_label)

        # --- tray icon --------------------------------------------------------
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip(TITLE)
        menu = QMenu()
        menu.addAction("Show console", self.show_console)
        menu.addAction("Restart bot", self.restart_bot)
        self.tray_unblock = menu.addAction("🔓 Unblock bridge", self.unblock)
        self.tray_unblock.setVisible(False)
        self.tray_regress = menu.addAction("🔓 Unlock & add regression", self.unblock_and_regress)
        self.tray_regress.setVisible(False)
        self.tray_wake = menu.addAction("☀️ Wake up (exit sleep)", self.wake)
        self.tray_wake.setVisible(False)
        self.tray_intrusion = menu.addAction("🛡 Intrusion Lock", self.toggle_intrusion)
        self.tray_intrusion.setCheckable(True)
        menu.addSeparator()
        menu.addAction("Quit", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        # Watch the firewall BLOCK flag and the SLEEP flag; reflect them in the tray.
        self._was_blocked = None
        self._was_sleeping = None
        self._block_timer = QTimer(self)
        self._block_timer.timeout.connect(self._check_block)
        self._block_timer.timeout.connect(self._check_sleep)
        self._block_timer.timeout.connect(self._refresh_intrusion)
        self._block_timer.start(2000)
        self._check_block()
        self._check_sleep()
        self._refresh_intrusion()

        self.start_bot()

    # --- firewall lock state -------------------------------------------------
    def _check_block(self) -> None:
        blocked = BLOCK_FILE.exists()
        for act in (self.act_unblock, self.tray_unblock, self.act_regress, self.tray_regress):
            act.setVisible(blocked)
        if blocked == self._was_blocked:
            return
        self._was_blocked = blocked
        if blocked:
            self.set_status("🔒 BLOCKED — hacking attempt flagged")
            self.append(
                "\n[supervisor] 🔒 BLOCKED — a request was flagged as a hacking attempt.\n"
                f"  Offending prompt: {self._block_reason()!r}\n"
                "  'UNBLOCK' = just resume · 'Unlock & add regression' = resume AND "
                "record this as a false positive so it never blocks again.\n"
            )
            self.tray.showMessage(
                f"{TITLE} LOCKED 🔒",
                "A request was flagged as a hacking attempt. Open the console and "
                "Unblock (or Unlock & add regression) when you're at the machine.",
                QSystemTrayIcon.Critical,
                10000,
            )
        else:
            self.set_status("running")

    def _block_reason(self) -> str:
        try:
            for line in BLOCK_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("reason:"):
                    return line[len("reason:"):].strip()
        except OSError:
            pass
        return ""

    def unblock(self) -> None:
        try:
            BLOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        self.append("\n[supervisor] 🔓 Unblocked at the machine — bridge will resume.\n")
        self._check_block()
        self.tray.showMessage(
            TITLE, "Unblocked — the bridge will resume.", self.icon, 4000
        )

    def unblock_and_regress(self) -> None:
        reason = self._block_reason()
        if reason:
            try:
                with open(REGRESSIONS_FILE, "a", encoding="utf-8") as f:
                    f.write(f'\n- "{reason}"\n')
            except OSError:
                self.append("\n[supervisor] ⚠️ Could not write to the regressions file.\n")
        try:
            BLOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        self.append(
            f"\n[supervisor] 🔓 Unblocked + recorded as a false positive: {reason[:100]!r}\n"
        )
        self._check_block()
        self.tray.showMessage(
            TITLE,
            "Unblocked and added to the regressions list — it won't block this again.",
            self.icon,
            5000,
        )

    # --- paranoid intrusion gate (toggle; default ON, disabled here only) -----
    def toggle_intrusion(self, *args) -> None:
        on = not INTRUSION_OFF_FILE.exists()  # current state
        try:
            if on:                                    # ON -> turn OFF
                INTRUSION_OFF_FILE.write_text("off\n", encoding="utf-8")
            else:                                     # OFF -> turn ON
                INTRUSION_OFF_FILE.unlink(missing_ok=True)
        except OSError:
            self.append("\n[supervisor] ⚠️ Could not toggle the intrusion-lock flag.\n")
        self._refresh_intrusion()
        now_on = not INTRUSION_OFF_FILE.exists()
        self.append(f"\n[supervisor] 🛡 Intrusion lock {'ON' if now_on else 'OFF'} "
                    "(set at the machine).\n")
        self.tray.showMessage(
            TITLE, f"Intrusion lock {'ON' if now_on else 'OFF'}.", self.icon, 4000)

    def _refresh_intrusion(self) -> None:
        on = not INTRUSION_OFF_FILE.exists()
        self.intrusion_btn.setChecked(on)
        self.intrusion_btn.setText(f"🛡 Intrusion Lock: {'ON' if on else 'OFF'}")
        self.intrusion_btn.setStyleSheet(INTRUSION_ON_CSS if on else INTRUSION_OFF_CSS)
        self.tray_intrusion.setChecked(on)
        self.tray_intrusion.setText(f"🛡 Intrusion lock: {'ON' if on else 'OFF'}")

    # --- sleep state (Telegram input paused; Claude keeps running) ------------
    def _check_sleep(self) -> None:
        sleeping = SLEEP_FILE.exists()
        for act in (self.act_wake, self.tray_wake):
            act.setVisible(sleeping)
        if sleeping == self._was_sleeping:
            return
        self._was_sleeping = sleeping
        if sleeping:
            self.set_status("😴 SLEEP — Telegram input paused")
            self.append(
                "\n[supervisor] 😴 SLEEP mode — Telegram input is paused (Claude keeps "
                "running). Click 'WAKE UP' to resume accepting input.\n"
            )
            self.tray.showMessage(
                f"{TITLE} — sleeping 😴",
                "Telegram input is paused. Click the tray icon → 'Wake up' to resume.",
                self.icon,
                6000,
            )
        elif not BLOCK_FILE.exists():
            self.set_status("running")

    def wake(self) -> None:
        try:
            SLEEP_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        self.append("\n[supervisor] ☀️ Woken at the machine — Telegram input resumes.\n")
        self._check_sleep()
        self.tray.showMessage(
            TITLE, "Awake — Telegram input resumes.", self.icon, 4000
        )

    # --- bot process management ----------------------------------------------
    def start_bot(self) -> None:
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        self.set_status("launching bot…")
        self.proc = QProcess(self)
        self.proc.setProgram(PYTHON)
        self.proc.setArguments([BOT])
        self.proc.setWorkingDirectory(str(HERE))
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")  # don't buffer the bot's logs
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.started.connect(lambda: self.set_status("running"))
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_proc_error)
        self._uptime.start()
        self.proc.start()

    def restart_bot(self) -> None:
        self._fast_fails = 0
        self.append("\n[supervisor] restart requested\n")
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.terminate()  # _on_finished will relaunch it
            if not self.proc.waitForFinished(5000):
                self.proc.kill()
        else:
            self.start_bot()

    @staticmethod
    def _open_log():
        try:
            # Big backstop only — the user clears manually via "Clear logs".
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 500_000_000:
                LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
            return open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        except OSError:
            return None

    def clear_logs(self) -> None:
        """Clear the console view AND truncate the persistent log file."""
        self.view.clear()
        try:
            if self._log_fh:
                self._log_fh.close()
            LOG_FILE.write_text("", encoding="utf-8")
            self._log_fh = self._open_log()
        except OSError:
            pass
        self.append("[supervisor] logs cleared.\n")

    def _on_output(self) -> None:
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        self.append(data)
        if self._log_fh is not None:
            try:
                self._log_fh.write(data)
                self._log_fh.flush()
            except OSError:
                pass

    def _on_finished(self, code: int, _status) -> None:
        self.set_status(f"stopped (exit {code})")
        if self._quitting:
            return
        if self._uptime.elapsed() < FAST_FAIL_SECS:
            self._fast_fails += 1
        else:
            self._fast_fails = 0
        if self._fast_fails > MAX_FAST_FAILS:
            self.append(
                "\n[supervisor] bot keeps exiting quickly — auto-restart paused. "
                "Fix the issue and click 'Restart bot'.\n"
            )
            self.set_status("crash loop — auto-restart paused")
            return
        delay = min(30, 3 * max(1, self._fast_fails)) * 1000
        self.append(f"\n[supervisor] bot exited; restarting in {delay // 1000}s…\n")
        QTimer.singleShot(delay, self._restart_if_running_session)

    def _restart_if_running_session(self) -> None:
        if not self._quitting:
            self.start_bot()

    def _on_proc_error(self, err) -> None:
        if err == QProcess.FailedToStart:
            self.append(
                f"\n[supervisor] FAILED TO START: {PYTHON} {BOT}\n"
                "Check that the virtualenv exists (run ./run.sh once).\n"
            )
            self.set_status("failed to start")

    # --- window / tray behaviour ---------------------------------------------
    def append(self, text: str) -> None:
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        self.view.setTextCursor(cur)
        self.view.insertPlainText(text)
        if at_bottom:
            sb.setValue(sb.maximum())

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.tray.setToolTip(f"{TITLE} — {text}")

    def show_console(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible():
                self.hide()
            else:
                self.show_console()

    def closeEvent(self, event) -> None:
        # Closing the window hides to tray instead of quitting.
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            TITLE,
            "Still running in the tray. Right-click the icon to quit.",
            self.icon,
            3000,
        )

    def quit_app(self) -> None:
        self._quitting = True
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.terminate()
            if not self.proc.waitForFinished(5000):
                self.proc.kill()
        self.tray.hide()
        if self._log_fh is not None:
            self._log_fh.close()
        QApplication.instance().quit()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(DESKTOP_NAME)          # distinct WM class -> separate taskbar entry
    app.setApplicationDisplayName(TITLE)
    app.setDesktopFileName(DESKTOP_NAME)          # matches this install's autostart .desktop
    app.setQuitOnLastWindowClosed(False)  # live in the tray

    # Single instance: if one is already running, just ask it to show, then exit.
    probe = QLocalSocket()
    probe.connectToServer(APP_ID)
    if probe.waitForConnected(200):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(200)
        probe.disconnectFromServer()
        return
    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)

    win = Supervisor()

    def on_new_connection() -> None:
        conn = server.nextPendingConnection()
        if conn:
            conn.readyRead.connect(lambda: (conn.readAll(), win.show_console()))

    server.newConnection.connect(on_new_connection)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        # No tray (rare here) — fall back to just showing the window.
        win.show_console()
        QMessageBox.information(
            win,
            TITLE,
            "No system tray detected; showing the console window directly.",
        )

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
