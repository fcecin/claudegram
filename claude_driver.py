"""
Drives a single persistent Claude Code instance via the Claude Agent SDK and
relays its activity as structured events. Telegram-agnostic on purpose — bot.py
turns these events into chat messages.

Guarantees:
  - cwd = the given working dir (created by the caller).
  - permission_mode="bypassPermissions" — full autonomy, asks nothing.
  - Uses the user's OAuth SUBSCRIPTION, never the metered API (API-key env vars
    are stripped before the SDK spawns the CLI).
  - RESUMES the previous session across restarts: the session id is persisted to
    a file and passed as `resume=` on connect, so context survives a reboot.
  - One turn at a time (a lock); interrupt() cancels a running turn.
  - Surfaces auto-compaction via a PreCompact hook (-> on_system callback).
"""

import asyncio
import json
import logging
import os
import shutil
import signal
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

log = logging.getLogger("claudegram")

# Task statuses that mean a background task (a run_in_background shell / agent) has FINISHED and must
# be cleared from the live "N shell(s)" picture. This spans both lifecycle vocabularies: a
# task_notification reports "stopped" (the CLI's mapped form of a killed task) while a task_updated
# patch reports the raw "killed" -- and a terminal state can arrive via EITHER message (a TaskStop'd
# task sometimes reports only task_updated status="killed", with the notification suppressed). Prefer
# the SDK's own constant so new statuses are tracked automatically; fall back to the full union for an
# older SDK. (The previous hand-rolled set missed "stopped", so every stopped/killed background shell
# leaked into the status line forever -- it said "2 shell(s) ... they'll wake me when they land" for
# builds that had already ended.)
try:
    from claude_agent_sdk import TERMINAL_TASK_STATUSES as _SDK_TERMINAL
except Exception:  # older SDK without the exported constant
    _SDK_TERMINAL = None
TERMINAL_TASK_STATUSES = frozenset(_SDK_TERMINAL or {"completed", "failed", "stopped", "killed"})

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# ask()'s stuck-turn safety net: if the stream goes TOTALLY silent (no message at all) for
# STUCK_SECS, the turn is released so the dispatcher isn't wedged forever. Module-level so
# tests can shrink them; long foreground tools still produce stream activity well under this.
STUCK_SECS = 900
STUCK_POLL_SECS = 30
# The SDK doesn't expose Claude Code's unset/default effort as a value, so we pin an
# explicit default — "effort" is then always a concrete, known level (override: bot effort).
DEFAULT_EFFORT = "high"

# fable is never an AMBIENT default: it runs only when explicitly chosen (a bot's
# config.json "model" or `bot model fable`). If the machine's env/settings default
# would resolve to fable, sessions without a forced model run this instead.
FABLE_GUARD_FALLBACK = "opus"


def ambient_default_model() -> str | None:
    """The model the CLI would pick with no --model: ANTHROPIC_MODEL env, then
    ~/.claude/settings.json — the CLI's own precedence, mirrored."""
    v = os.environ.get("ANTHROPIC_MODEL")
    if v:
        return v
    try:
        return json.loads(
            (Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")
        ).get("model")
    except Exception:
        return None


def default_model_guard() -> str | None:
    """The explicit --model to force when a session has NO forced model: normally
    None (let the CLI pick), but never let the ambient default land on fable."""
    if "fable" in (ambient_default_model() or "").lower():
        return FABLE_GUARD_FALLBACK
    return None


def _proc_ppid_cmd(pid: int):
    """(ppid, cmdline) for a pid, parsed robustly (comm may contain spaces)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        ppid = int(data[data.rfind(")") + 2:].split()[1])
    except (OSError, IndexError, ValueError):
        return None, ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        cmd = ""
    return ppid, cmd


def _children_map() -> dict:
    m: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        ppid, _ = _proc_ppid_cmd(int(entry))
        if ppid is not None:
            m.setdefault(ppid, []).append(int(entry))
    return m


def sigkill_subtree(root: int | None) -> list[int]:
    """SIGKILL a specific pid and every descendant of it. This is the per-session kill:
    a controller passes its OWN CLI subprocess pid so it never touches a sibling session's
    subprocess (under multiplexing, all sessions' CLI children share bot.py as parent, so a
    process-wide 'kill every claude child' would nuke the whole fleet — see kill())."""
    if not root:
        return []
    cmap = _children_map()
    killed: list[int] = []
    targets, stack = [root], list(cmap.get(root, []))
    while stack:
        p = stack.pop()
        targets.append(p)
        stack.extend(cmap.get(p, []))
    for t in targets:
        try:
            os.kill(t, signal.SIGKILL)
            killed.append(t)
        except OSError:
            pass
    return killed


def sigkill_claude_subtree() -> list[int]:
    """SIGKILL every 'claude' CLI child of THIS process and all their descendants. This is
    the process-WIDE nuke (all sessions at once); a single controller must NOT use it — it
    kills its siblings too. Used only by the PANIC paths (`bot lock` and the intrusion
    hard-lock in bot.py), as belt-and-suspenders after the per-session kills: it catches any
    stray CLI child no live controller tracks."""
    me = os.getpid()
    cmap = _children_map()
    killed: list[int] = []
    for child in cmap.get(me, []):
        _, cmd = _proc_ppid_cmd(child)
        if "claude" not in cmd.lower():
            continue
        killed.extend(sigkill_subtree(child))
    return killed

_API_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def force_subscription_env() -> list[str]:
    """Strip API-routing env vars so the instance uses the subscription."""
    return [v for v in _API_ENV_VARS if os.environ.pop(v, None) is not None]


def summarize_tool(name: str, inp: dict) -> str:
    """A short, human one-liner describing a tool call, for the activity feed."""
    def short(s, n=160):
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if name == "Bash":
        return f"🔧 Bash: {short(inp.get('command', ''))}"
    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        return f"📝 {name}: {inp.get('file_path') or inp.get('notebook_path', '')}"
    if name == "Read":
        return f"📖 Read: {inp.get('file_path', '')}"
    if name == "Grep":
        return f"🔎 Grep: {short(inp.get('pattern', ''), 80)}"
    if name == "Glob":
        return f"🔎 Glob: {short(inp.get('pattern', ''), 80)}"
    if name in ("WebFetch", "WebSearch"):
        return f"🌐 {name}: {short(inp.get('url') or inp.get('query', ''), 80)}"
    if name == "Task":
        return f"🤖 Subagent: {short(inp.get('description', ''), 80)}"
    if name == "TodoWrite":
        return "🗒 Updating todo list"
    return f"⚙️ {name}"


def _migrate_session(session_id: str, old_cwd: str, new_cwd: str) -> None:
    """Copy a conversation transcript so the SAME session id is resumable from
    new_cwd. Claude keys sessions by directory; we copy the .jsonl (the latest
    state, from wherever we currently are) into new_cwd's project space, OVERWRITING
    any older copy there — so moving dirs never loses or regresses the conversation."""
    try:
        from claude_agent_sdk import project_key_for_directory
        projects = Path.home() / ".claude" / "projects"
        src = projects / project_key_for_directory(old_cwd) / f"{session_id}.jsonl"
        dst_dir = projects / project_key_for_directory(new_cwd)
        dst = dst_dir / f"{session_id}.jsonl"
        if not src.exists():
            log.warning("Migrate: no transcript at %s — nothing to carry over.", src)
            return
        existed = dst.exists()
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log.info("Migrated session %s -> %s%s", session_id, dst,
                 " (overwrote older copy)" if existed else "")
    except Exception:
        log.exception("Session migration failed")


class ClaudeController:
    """A long-lived, multi-turn Claude Code session that resumes across restarts."""

    def __init__(self, cwd: str, session_file: str, cwd_file: str | None = None,
                 model: str | None = None, max_budget_usd: float | None = None,
                 effort: str | None = None) -> None:
        self.session_file = Path(session_file)
        self.cwd_file = Path(cwd_file) if cwd_file else None
        self._default_cwd = str(cwd)
        self.cwd = self._load_cwd() or str(cwd)
        self.session_id = self._load_session()
        # Spawn value: config effort if valid, else the immutable code default. Runtime
        # `bot effort` changes it in-memory only (process-lived) — never persisted, so a
        # restart reverts to config/default. Uniform with model and transcribe.
        self.effort = (effort if effort in VALID_EFFORTS else None) or DEFAULT_EFFORT
        self.forced_model = model
        self.max_budget_usd = max_budget_usd
        self.model = None  # actual model, captured from the init message
        self._client: ClaudeSDKClient | None = None
        self._child_pid: int | None = None  # this session's OWN CLI subprocess (per-session kill)
        self._interrupted = False  # set by interrupt_turn(); the in-flight dispatch consumes it
                                   # so a user interrupt closes cleanly (not as a crash/error)
        self._stuck_release = False  # set when ask() gives up on a silent turn (STUCK_SECS);
                                     # consumed by the dispatch so it never claims "Done"
        self._lock = asyncio.Lock()  # serializes USER turns (not the reader)
        self._on_system = None  # async callback(kind:str, data:dict) for the active turn

        # --- continuous watchdog of the Claude instance --------------------------
        # The bridge is a MONITOR: one persistent reader drains the SDK stream forever,
        # so it sees not only replies to our queries but also the turns Claude starts on
        # its OWN when a background shell completes ("the build landed"). It never walks
        # away at a ResultMessage, so it never inserts fake idleness.
        self._reader_task = None          # the always-on receive_messages() loop
        self._user_sink = None            # async fn(msg): renders the current USER turn
        self._spontaneous_sink = None     # async fn(msg): renders self-started turns
        self.in_segment = False           # True while Claude is actively thinking/typing
        self._cur_is_user = False         # is the in-flight segment a reply to our query?
        self._awaiting_user_segment = False  # we queried; next segment is ours
        self._segment_done = asyncio.Event()  # set when the current user segment ends
        self.last_activity = 0.0          # monotonic ts of the last stream message
        self.shells: dict[str, dict] = {}  # task_id -> {desc, type}: live background work
        self._seg_started_ts = 0.0        # monotonic ts the current segment began

    @property
    def busy(self) -> bool:
        """Claude is actively producing a turn right now (thinking/typing/tooling)."""
        return self.in_segment

    # --- session persistence (resume across process restarts) ----------------
    def _load_session(self) -> str | None:
        try:
            sid = self.session_file.read_text(encoding="utf-8").strip()
            return sid or None
        except OSError:
            return None

    def _save_session(self) -> None:
        try:
            if self.session_id:
                self.session_file.write_text(self.session_id, encoding="utf-8")
            elif self.session_file.exists():
                self.session_file.unlink()
        except OSError:
            pass

    # --- effort level (process-lived; applied on connect) ---------------------
    def get_effort(self) -> str | None:
        return self.effort

    # --- working directory (persisted; ties to the session space) -------------
    def _load_cwd(self) -> str | None:
        if not self.cwd_file:
            return None
        try:
            v = self.cwd_file.read_text(encoding="utf-8").strip()
            return v or None
        except OSError:
            return None

    def _save_cwd(self) -> None:
        if not self.cwd_file:
            return
        try:
            self.cwd_file.write_text(self.cwd, encoding="utf-8")
        except OSError:
            pass

    def get_cwd(self) -> str:
        return self.cwd

    async def set_cwd(self, path: str) -> bool:
        """Switch Claude's working directory, MIGRATING the current conversation so
        the SAME session id resumes there. The conversation follows you across dirs
        and never resets on a move. Returns False on error."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(self.cwd).expanduser() / p
        p = p.resolve()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        async with self._lock:
            old_cwd, new_cwd = self.cwd, str(p)
            if self.session_id and new_cwd != old_cwd:
                _migrate_session(self.session_id, old_cwd, new_cwd)
            self.cwd = new_cwd
            self._save_cwd()
            # session_id is KEPT — same conversation, now resumable from new_cwd.
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None
        return True

    async def set_effort(self, level: str) -> bool:
        """Set the reasoning effort for subsequent turns (in-memory, process-lived — not
        persisted, so a restart reverts to config/default). Reconnects (resuming the same
        session) so it takes effect going forward. Returns False if invalid."""
        level = (level or "").strip().lower()
        if level not in VALID_EFFORTS:
            return False
        async with self._lock:
            self.effort = level
            # Drop the client so the next turn reconnects with the new effort,
            # resuming the same conversation.
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None
        return True

    async def set_model(self, model: str | None) -> bool:
        """Override the model for subsequent turns (None => revert to the default).
        Like set_effort, drops the client under the lock so it reconnects (resuming the
        same session) AFTER any in-flight turn — never mid-turn. Returns True."""
        async with self._lock:
            self.forced_model = model or None
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None
        return True

    # --- compaction hook ------------------------------------------------------
    async def _pre_compact_hook(self, input_data, tool_use_id, context):
        if self._on_system is not None:
            trigger = "auto"
            if isinstance(input_data, dict):
                trigger = input_data.get("trigger", "auto")
            else:
                trigger = getattr(input_data, "trigger", "auto")
            try:
                await self._on_system("compaction_started", {"trigger": trigger})
            except Exception:
                pass
        return {}

    def _build_options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=self.cwd,
            permission_mode="bypassPermissions",
            resume=self.session_id,  # None => fresh session
            effort=self.effort,  # None => model/CLI default
            model=self.forced_model or default_model_guard(),  # guard: default never fable
            max_budget_usd=self.max_budget_usd,  # None => no cap; 0 => never spend money
            include_partial_messages=True,  # stream text deltas for live output
            hooks={"PreCompact": [HookMatcher(hooks=[self._pre_compact_hook])]},
        )

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        try:
            os.makedirs(self.cwd, exist_ok=True)  # SDK requires cwd to exist
        except OSError:
            pass
        if self.session_id:
            log.info("Resuming session %s (cwd=%s)", self.session_id, self.cwd)
        else:
            log.info("Starting a fresh session (cwd=%s)", self.cwd)
        try:
            self._client = ClaudeSDKClient(options=self._build_options())
            await self._client.connect()
        except Exception as e:
            # A stale/missing saved session can make resume fail — retry fresh once,
            # loudly (never forget silently).
            self._client = None
            if self.session_id:
                log.warning(
                    "Resume of session %s FAILED (%s) — starting fresh; prior "
                    "history may be unavailable.", self.session_id, e,
                )
                self.session_id = None
                self._save_session()
                self._client = ClaudeSDKClient(options=self._build_options())
                await self._client.connect()
            else:
                raise
        # Remember THIS client's own CLI subprocess so kill() can target just our subtree
        # (not every session's). Best-effort: the SDK spawns it inside connect().
        self._child_pid = self._live_child_pid()
        # (Re)start the always-on reader on the fresh client.
        self._start_reader()
        # Announce the (re)started session to the active turn's listener.
        if self._on_system is not None:
            try:
                await self._on_system(
                    "session_started", {"id": self.session_id, "cwd": self.cwd}
                )
            except Exception:
                pass

    def set_spontaneous_handler(self, handler) -> None:
        """Register the async fn(message) that renders turns Claude starts on its own
        (e.g. when a background shell completes). Called for every message of a
        self-initiated segment, including its init (start) and ResultMessage (end)."""
        self._spontaneous_sink = handler

    # --- the always-on reader (watchdog) -------------------------------------
    def _start_reader(self) -> None:
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._read_loop())

    def _stop_reader(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

    def _track(self, message) -> None:
        """Update the live picture of the Claude instance from one stream message:
        background-shell set, segment boundaries, session id, model."""
        self.last_activity = asyncio.get_event_loop().time()
        kind = type(message).__name__
        data = getattr(message, "data", None) or {}
        sid = getattr(message, "session_id", None) or data.get("session_id")
        if sid and sid != self.session_id:
            self.session_id = sid
            self._save_session()
        # Task fields are direct dataclass attributes on the SDK message; the SystemMessage base also
        # mirrors them into `data`. Prefer the attribute, fall back to `data`, so a payload shape
        # change on either side does not silently drop tracking.
        if kind == "TaskStartedMessage":
            tid = getattr(message, "task_id", None) or data.get("task_id")
            if tid:
                self.shells[tid] = {
                    "desc": getattr(message, "description", None) or data.get("description") or "(task)",
                    "type": getattr(message, "task_type", None) or data.get("task_type") or "task",
                }
        elif kind == "TaskNotificationMessage":
            # Every TaskNotificationStatus (completed / failed / stopped) is terminal, so a
            # notification for a task ALWAYS means it has ended -- clear it unconditionally.
            tid = getattr(message, "task_id", None) or data.get("task_id")
            if tid:
                self.shells.pop(tid, None)
        elif kind == "TaskUpdatedMessage":
            # A task_updated carries lifecycle changes; clear only on a terminal status (which is how
            # a TaskStop'd task -- status="killed" -- reports when its notification is suppressed).
            tid = getattr(message, "task_id", None) or data.get("task_id")
            status = (
                getattr(message, "status", None)
                or data.get("status")
                or (getattr(message, "patch", None) or data.get("patch") or {}).get("status")
            )
            status = str(status).lower() if status else None
            if tid and status in TERMINAL_TASK_STATUSES:
                self.shells.pop(tid, None)

    async def _read_loop(self) -> None:
        """Drain the SDK stream forever. Route each segment to the right renderer:
        a segment that follows one of our queries is a USER turn; any other segment
        is one Claude started on its own (relayed via the spontaneous sink)."""
        client = self._client
        try:
            async for message in client.receive_messages():
                self._track(message)
                kind = type(message).__name__
                sub = getattr(message, "subtype", None)
                # Segment start: every turn (ours or self-started) opens with init.
                if kind == "SystemMessage" and sub == "init":
                    self.in_segment = True
                    self._seg_started_ts = asyncio.get_event_loop().time()
                    self._cur_is_user = self._awaiting_user_segment
                    self._awaiting_user_segment = False
                    mdl = (getattr(message, "data", None) or {}).get("model")
                    if mdl:
                        self.model = mdl
                elif not self.in_segment and kind in (
                    "AssistantMessage", "UserMessage", "StreamEvent"
                ):
                    # Defensive: content without a preceding init — open a segment.
                    self.in_segment = True
                    self._seg_started_ts = asyncio.get_event_loop().time()
                    self._cur_is_user = self._awaiting_user_segment
                    self._awaiting_user_segment = False

                sink = self._user_sink if self._cur_is_user else self._spontaneous_sink
                if sink is not None:
                    try:
                        await sink(message)
                    except Exception:
                        log.exception("stream sink error")

                if kind == "ResultMessage":
                    self.in_segment = False
                    if self._cur_is_user:
                        self._segment_done.set()
                    self._cur_is_user = False
        except asyncio.CancelledError:
            return
        except Exception:
            # The CLI subprocess died out from under us (crash, OOM kill, or a sibling
            # teardown). Self-heal: drop the dead client so the NEXT ask() reconnects and
            # resumes (session_id is persisted), and release any ask() waiting on this turn
            # instead of letting it hang to the 900s stuck-timeout. Guard on identity so we
            # never clobber a client a concurrent reconnect already swapped in.
            log.exception("reader loop ended — dropping the dead client so the next turn reconnects")
            if self._client is client:
                self._client = None
                self._child_pid = None
                self._reset_live_state()  # clears shells + sets _segment_done (frees a waiting ask)

    def status(self) -> dict:
        """A snapshot of the Claude INSTANCE state, for the watchdog poll."""
        now = asyncio.get_event_loop().time()
        return {
            "active": self.in_segment,
            "segment_secs": (now - self._seg_started_ts) if self.in_segment else 0,
            "shells": [dict(v) for v in self.shells.values()],
            "idle_secs": now - self.last_activity if self.last_activity else 0,
            "connected": self._client is not None,
        }

    # --- turns ---------------------------------------------------------------
    async def ask(self, prompt: str, on_event, on_system=None) -> None:
        """Send a user prompt and return when ITS reply turn ends. The reader keeps
        running afterwards, so any turns Claude later starts on its own are still
        relayed (via the spontaneous sink) without another user message."""
        async with self._lock:
            self._on_system = on_system
            self._user_sink = on_event
            try:
                await self._ensure_connected()
                self._segment_done.clear()
                self._interrupted = False     # fresh turn — clear any stale interrupt flag
                self._stuck_release = False   # ...and any stale stuck flag
                self._awaiting_user_segment = True
                await self._client.query(prompt)
                # Wait for the turn to end — but never hang forever. If the stream goes
                # totally silent (no message at all) for STUCK_SECS, give up and release
                # the turn so the dispatcher isn't wedged. The release is FLAGGED
                # (_stuck_release) so the dispatch reports it honestly instead of "Done".
                loop = asyncio.get_event_loop()
                # A dead-silent CLI on a fresh connection leaves last_activity at 0, which
                # would read as "no idleness" forever — anchor the clock at query time.
                if not self.last_activity:
                    self.last_activity = loop.time()
                while not self._segment_done.is_set():
                    try:
                        await asyncio.wait_for(self._segment_done.wait(), STUCK_POLL_SECS)
                    except asyncio.TimeoutError:
                        idle = loop.time() - self.last_activity
                        if idle >= STUCK_SECS:
                            log.warning("ask: no stream activity for %.0fs and no result — "
                                        "releasing the turn (assumed stuck)", idle)
                            self._stuck_release = True
                            break
            finally:
                self._awaiting_user_segment = False
                self._user_sink = None
                self._on_system = None

    async def context_usage(self):
        """Best-effort context-window usage (dict) or None."""
        if self._client is None:
            return None
        try:
            return await asyncio.wait_for(self._client.get_context_usage(), 8)
        except Exception:
            return None

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def interrupt_turn(self, settle: float = 10.0) -> bool:
        """Bare Esc/Ctrl-C: stop the CURRENT turn but KEEP the CLI connected — background shells
        and the session context survive. This is the LIGHTEST of the three teardowns:
          - interrupt_turn(): stop the turn, keep everything (bg + session).            [this]
          - stop():           interrupt + DISCONNECT (drops bg; resumes fresh next ask).
          - kill():           SIGKILL the subtree (bg dies hard; resumes next ask).
        Returns True if a turn was actually interrupted, False if nothing was running.
        Deliberately does NOT take _lock (a live turn holds it), like stop()/kill()."""
        client = self._client
        # A turn is interruptible from the moment its query() is sent — including the window
        # BEFORE the CLI emits the init message (in_segment still False but a user turn is in
        # flight, tracked by _awaiting_user_segment). The old in_segment-only check answered
        # "nothing to interrupt" during that window while a turn was actually pending.
        if client is None or not (self.in_segment or self._awaiting_user_segment):
            return False
        self._interrupted = True  # so the in-flight dispatch closes this turn cleanly, not as a crash
        try:
            await asyncio.wait_for(client.interrupt(), 5)
        except Exception:
            log.warning("interrupt_turn: interrupt() failed/timed out — forcing turn end")
        # Prefer a clean end: interrupting makes the CLI emit this turn's ResultMessage, which the
        # (still-running) reader routes -> _segment_done, releasing the waiting ask() and keeping
        # segment bookkeeping consistent. Wait for that. If the CLI never closes the turn (the
        # historical wedge), release it ourselves — WITHOUT dropping the client, so bg + context
        # stay alive. (The timeout path implies the ORIGINAL turn is still stuck: a new turn can
        # only start after the old ask() is released, which sets _segment_done and ends this wait.)
        try:
            await asyncio.wait_for(self._segment_done.wait(), settle)
        except asyncio.TimeoutError:
            log.warning("interrupt_turn: no turn-end %.0fs after interrupt — releasing locally", settle)
            self._end_segment_locally()
        return True

    def consume_interrupt_flag(self) -> bool:
        """True exactly once if the last/current turn was ended by interrupt_turn() — lets the
        in-flight dispatch render it as a clean stop rather than a crash. Self-clearing."""
        was = self._interrupted
        self._interrupted = False
        return was

    def consume_stuck_flag(self) -> bool:
        """True exactly once if the last turn was RELEASED AS STUCK by ask()'s silence net —
        lets the dispatch report the release instead of claiming success. Self-clearing."""
        was = self._stuck_release
        self._stuck_release = False
        return was

    def _end_segment_locally(self) -> None:
        """Release a stuck ask() and reset SEGMENT bookkeeping WITHOUT touching the client,
        reader, or shells — so background work and the session survive. Contrast
        _reset_live_state(), which forgets everything (incl. shells) because the CLI itself is
        going away."""
        self.in_segment = False
        self._cur_is_user = False
        self._awaiting_user_segment = False
        self._segment_done.set()

    async def stop(self) -> None:
        """Graceful stop for `bot stop`: interrupt the running turn, then reset to a CLEAN
        state — release any ask() waiting on _segment_done, restart the reader, and drop the
        client so the NEXT turn reconnects and RESUMES the session. This mirrors kill()
        (which works) but without SIGKILL. A bare interrupt() left the dispatcher wedged;
        this does the same cleanup kill() does, so the bridge keeps accepting input."""
        client = self._client
        if client is not None:
            try:
                await asyncio.wait_for(client.interrupt(), 5)
            except Exception:
                log.warning("stop: interrupt failed/timed out — resetting anyway")
        self._stop_reader()
        self._reset_live_state()  # sets _segment_done (frees a waiting ask) + clears state
        self._client = None       # next ask reconnects + resumes (session_id persisted)
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 3)
            except Exception:
                pass

    def _live_child_pid(self) -> int | None:
        """This client's OWN CLI subprocess pid, dug out of the SDK transport. Best-effort:
        internal SDK attributes, so guarded — a None just means kill() skips the SIGKILL and
        relies on disconnect()'s own terminate/kill."""
        try:
            return self._client._transport._process.pid  # type: ignore[attr-defined]
        except Exception:
            return None

    async def kill(self) -> list[int]:
        """Hard-kill (SIGKILL) THIS session's Claude CLI subprocess (and its descendants) —
        never a sibling session's. The next turn reconnects, resuming the session. Deliberately
        does NOT take the lock, so it works even when a turn is stuck."""
        killed = sigkill_subtree(self._child_pid or self._live_child_pid())
        client = self._client
        self._stop_reader()
        self._reset_live_state()
        self._client = None  # force a reconnect on the next ask()
        self._child_pid = None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 3)
            except Exception:
                pass
        return killed

    async def reset(self) -> None:
        """Drop the conversation and start a brand-new session next time."""
        if self._client is not None and self.busy:
            try:
                await self._client.interrupt()
            except Exception:
                pass
        async with self._lock:
            self._stop_reader()
            self._reset_live_state()
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None
            self.session_id = None
            self._save_session()

    def _reset_live_state(self) -> None:
        """Forget the live picture when the CLI goes away (its background shells die
        with it). A fresh connection starts with a clean watchdog view."""
        self.in_segment = False
        self._cur_is_user = False
        self._awaiting_user_segment = False
        self.shells.clear()
        self._segment_done.set()  # release any ask() waiting on a now-dead client
