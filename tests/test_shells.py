"""Regression for the live-shell tracker (ClaudeController._track).

THE BUG it locks down: a background task (a run_in_background shell / agent) that is killed or
stopped -- e.g. at a session boundary, or via TaskStop -- notifies with status "stopped" (the CLI's
mapped form) or "killed" (the raw task_updated form). The old terminal-status set was
{completed, failed, cancelled, killed, error} -- it MISSED "stopped", so a stopped background shell
was never cleared from self.shells and leaked into the "N shell(s) ... they'll wake me when they
land" status line forever, for builds that had already ended.

The fix: use the SDK's own TERMINAL_TASK_STATUSES ({completed, failed, stopped, killed}), treat every
task_notification as terminal (all its statuses are), and clear on a terminal task_updated too.
"""
import claude_driver
from claude_agent_sdk import (
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)


def _controller():
    # _track only touches shells / session_id / last_activity, so build the controller without its
    # __init__ (which does file / session I/O) and set just those fields.
    c = claude_driver.ClaudeController.__new__(claude_driver.ClaudeController)
    c.shells = {}
    c.session_id = "s1"          # matches the messages below, so _save_session never fires
    c.session_file = None
    c.last_activity = 0.0
    return c


def _started(tid, desc="build"):
    return TaskStartedMessage(
        subtype="task_started",
        data={"task_id": tid, "description": desc},
        task_id=tid, description=desc, uuid="u", session_id="s1",
    )


def _notify(tid, status):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={"task_id": tid, "status": status},
        task_id=tid, status=status,
        output_file="/tmp/x.output", summary="", uuid="u", session_id="s1",
    )


def _updated(tid, status):
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={"task_id": tid, "patch": {"status": status}},
        task_id=tid, patch={"status": status}, status=status, session_id="s1",
    )


async def test_stopped_notification_clears_shell():
    c = _controller()
    c._track(_started("t1", "./build.sh > /tmp/m5.log"))
    assert "t1" in c.shells
    c._track(_notify("t1", "stopped"))     # the exact status a killed background shell emits
    assert "t1" not in c.shells, "a 'stopped' notification must clear the shell (the original bug)"


async def test_killed_via_task_updated_clears_shell():
    # A TaskStop'd task can report ONLY via task_updated status="killed" (notification suppressed).
    c = _controller()
    c._track(_started("t2"))
    c._track(_updated("t2", "killed"))
    assert "t2" not in c.shells


async def test_completed_and_failed_clear_shell():
    c = _controller()
    c._track(_started("t3"))
    c._track(_notify("t3", "completed"))
    assert "t3" not in c.shells
    c._track(_started("t4"))
    c._track(_notify("t4", "failed"))
    assert "t4" not in c.shells


async def test_running_update_does_not_clear_shell():
    # A non-terminal task_updated (running / pending / paused) must NOT drop a live shell.
    c = _controller()
    c._track(_started("t5"))
    c._track(_updated("t5", "running"))
    assert "t5" in c.shells, "a non-terminal update must not clear a live shell"
    c._track(_updated("t5", "completed"))
    assert "t5" not in c.shells


def test_terminal_set_covers_both_lifecycle_vocabularies():
    # Guard against a future edit narrowing the set: task_notification says "stopped",
    # task_updated says "killed"; both, plus completed/failed, must be terminal.
    for s in ("completed", "failed", "stopped", "killed"):
        assert s in claude_driver.TERMINAL_TASK_STATUSES
