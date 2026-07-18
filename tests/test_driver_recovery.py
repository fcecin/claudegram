"""Regressions for two single-session relics that multiplexing broke (2026-07-03).

THE BUGS these lock down:

1. `bot nostall off` crashed the MAIN bot. Ending the guard session called
   controller.kill(), which SIGKILLed *every* 'claude' CLI child of bot.py and all their
   descendants — a process-wide nuke written when there was only one CLI child. Under
   multiplexing every session's CLI is a child of bot.py, so killing the guard also killed
   the worker (log: `exit code -9`). The fix: kill() targets ONLY this session's own
   subprocess subtree (`sigkill_subtree(self._child_pid)`), never a sibling's.

2. After that kill, the bridge never recovered. The worker's reader loop caught the
   subprocess death and just returned, leaving self._client pointing at the dead process —
   so every later turn did query() on it → "Cannot write to terminated process", forever.
   The fix: a dead reader drops the client (self._client = None) so the next ask()
   reconnects and resumes, and releases any ask() waiting on the turn.
"""
import asyncio

import claude_driver


def _bare_controller():
    # Build without __init__ (which does file/session I/O); set only the fields the paths
    # under test touch.
    c = claude_driver.ClaudeController.__new__(claude_driver.ClaudeController)
    c._client = None
    c._child_pid = None
    c._reader_task = None
    c.shells = {}
    c.in_segment = False
    c._cur_is_user = False
    c._awaiting_user_segment = False
    c._segment_done = asyncio.Event()
    c.last_activity = 0.0
    return c


# --- 1. per-session kill --------------------------------------------------------

def test_sigkill_subtree_only_touches_its_own_descendants():
    # 100 is our session's CLI (children 101/102, grandchild 103). 200 is a SIBLING
    # session's CLI — it and its child 201 must survive.
    tree = {100: [101, 102], 101: [103], 200: [201]}
    killed = []
    orig_map, orig_kill = claude_driver._children_map, claude_driver.os.kill
    claude_driver._children_map = lambda: tree
    claude_driver.os.kill = lambda pid, sig: killed.append(pid)
    try:
        got = claude_driver.sigkill_subtree(100)
    finally:
        claude_driver._children_map, claude_driver.os.kill = orig_map, orig_kill
    assert set(got) == {100, 101, 102, 103}
    assert 200 not in got and 201 not in got   # a sibling session is never touched


def test_sigkill_subtree_none_is_a_noop():
    assert claude_driver.sigkill_subtree(None) == []
    assert claude_driver.sigkill_subtree(0) == []


async def test_kill_scopes_to_this_sessions_child_pid():
    c = _bare_controller()
    c._child_pid = 4242
    seen = []
    orig = claude_driver.sigkill_subtree
    claude_driver.sigkill_subtree = lambda root: (seen.append(root) or [])
    try:
        await c.kill()
    finally:
        claude_driver.sigkill_subtree = orig
    assert seen == [4242]                       # its OWN subtree, not the global claude nuke
    assert c._client is None and c._child_pid is None


def test_global_nuke_fans_out_over_every_claude_child():
    # The process-wide panic path still kills ALL claude children (each via sigkill_subtree),
    # but a non-claude child is left alone.
    me = 1
    tree = {me: [10, 20, 30], 10: [11]}
    cmds = {10: "node claude cli", 20: "claude", 30: "python transcribe_worker.py", 11: "sh"}
    killed = []
    o_getpid, o_map, o_cmd, o_kill = (
        claude_driver.os.getpid, claude_driver._children_map,
        claude_driver._proc_ppid_cmd, claude_driver.os.kill,
    )
    claude_driver.os.getpid = lambda: me
    claude_driver._children_map = lambda: tree
    claude_driver._proc_ppid_cmd = lambda p: (None, cmds.get(p, ""))
    claude_driver.os.kill = lambda pid, sig: killed.append(pid)
    try:
        got = claude_driver.sigkill_claude_subtree()
    finally:
        (claude_driver.os.getpid, claude_driver._children_map,
         claude_driver._proc_ppid_cmd, claude_driver.os.kill) = o_getpid, o_map, o_cmd, o_kill
    assert set(got) == {10, 11, 20}             # both claude subtrees
    assert 30 not in got                        # the transcribe worker is not a claude child


# --- 2. self-healing reader -----------------------------------------------------

async def test_reader_death_drops_client_and_releases_waiting_ask():
    c = _bare_controller()

    class _DeadClient:
        def receive_messages(self):
            async def _gen():
                if False:
                    yield None                  # make it an async generator
                raise RuntimeError("subprocess died (exit -9)")
            return _gen()

    c._client = _DeadClient()
    c._child_pid = 999
    c._segment_done.clear()                     # as if an ask() is waiting on this turn
    await c._read_loop()
    assert c._client is None                    # self-healed: the next ask() reconnects+resumes
    assert c._child_pid is None
    assert c._segment_done.is_set()             # the waiting ask() is freed, not left to hang


async def test_reader_death_does_not_clobber_a_reconnected_client():
    # If a concurrent reconnect swaps in a fresh client while the old reader is dying, the
    # old reader's error handler must NOT null it out (identity guard).
    c = _bare_controller()
    fresh = object()

    class _SwappingDeadClient:
        def receive_messages(self):
            async def _gen():
                c._client = fresh               # a reconnect swaps in a new client mid-loop
                if False:
                    yield None
                raise RuntimeError("boom")
            return _gen()

    c._client = _SwappingDeadClient()           # this is what _read_loop captures as `client`
    await c._read_loop()
    assert c._client is fresh                    # the live client is preserved (identity guard)


# --- 3. stuck-turn release is flagged, not silent --------------------------------

async def test_ask_stuck_release_sets_consumable_flag():
    # When the silence net fires, ask() must FLAG the release so the dispatch can report
    # it honestly (the old silent break let the turn render as "✅ Done").
    c = _bare_controller()
    c._lock = asyncio.Lock()
    c._on_system = None
    c._user_sink = None
    c._interrupted = False
    c._stuck_release = False

    class _SilentClient:
        async def query(self, prompt):
            pass                                     # accepts the prompt, then says nothing

    c._client = _SilentClient()
    old_secs, old_poll = claude_driver.STUCK_SECS, claude_driver.STUCK_POLL_SECS
    claude_driver.STUCK_SECS, claude_driver.STUCK_POLL_SECS = 0.05, 0.01
    try:
        await c.ask("hello", on_event=None)          # returns via the stuck release
    finally:
        claude_driver.STUCK_SECS, claude_driver.STUCK_POLL_SECS = old_secs, old_poll
    assert c.consume_stuck_flag() is True
    assert c.consume_stuck_flag() is False           # self-clearing, like the interrupt flag
