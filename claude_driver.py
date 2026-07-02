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
import logging
import os
import shutil
import signal
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

log = logging.getLogger("claudegram")

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The SDK doesn't expose Claude Code's unset/default effort as a value, so we pin an
# explicit default — "effort" is then always a concrete, known level (override: bot effort).
DEFAULT_EFFORT = "high"


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


def sigkill_claude_subtree() -> list[int]:
    """SIGKILL the 'claude' CLI child of THIS process and all its descendants."""
    me = os.getpid()
    cmap = _children_map()
    killed: list[int] = []
    for child in cmap.get(me, []):
        _, cmd = _proc_ppid_cmd(child)
        if "claude" not in cmd.lower():
            continue
        targets, stack = [child], list(cmap.get(child, []))
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

    def __init__(self, cwd: str, session_file: str, effort_file: str | None = None,
                 cwd_file: str | None = None, model: str | None = None,
                 max_budget_usd: float | None = None, effort: str | None = None) -> None:
        self.session_file = Path(session_file)
        self.effort_file = Path(effort_file) if effort_file else None
        self.cwd_file = Path(cwd_file) if cwd_file else None
        self._default_cwd = str(cwd)
        self.cwd = self._load_cwd() or str(cwd)
        self.session_id = self._load_session()
        self.effort = (self._load_effort()
                       or (effort if effort in VALID_EFFORTS else None)
                       or DEFAULT_EFFORT)
        self.forced_model = model
        self.max_budget_usd = max_budget_usd
        self.model = None  # actual model, captured from the init message
        self._client: ClaudeSDKClient | None = None
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

    # --- effort level (persisted; applied on connect) ------------------------
    def _load_effort(self) -> str | None:
        if not self.effort_file:
            return None
        try:
            val = self.effort_file.read_text(encoding="utf-8").strip().lower()
            return val if val in VALID_EFFORTS else None
        except OSError:
            return None

    def _save_effort(self) -> None:
        if not self.effort_file:
            return
        try:
            if self.effort:
                self.effort_file.write_text(self.effort, encoding="utf-8")
            elif self.effort_file.exists():
                self.effort_file.unlink()
        except OSError:
            pass

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
        """Set the reasoning effort for subsequent turns. Reconnects (resuming the
        same session) so it takes effect going forward. Returns False if invalid."""
        level = (level or "").strip().lower()
        if level not in VALID_EFFORTS:
            return False
        async with self._lock:
            self.effort = level
            self._save_effort()
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
            model=self.forced_model,  # None => whatever the user's env/CLI default is
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
        if kind == "TaskStartedMessage":
            tid = data.get("task_id")
            if tid:
                self.shells[tid] = {
                    "desc": data.get("description") or "(task)",
                    "type": data.get("task_type") or "task",
                }
        elif kind in ("TaskUpdatedMessage", "TaskNotificationMessage"):
            tid = data.get("task_id")
            status = data.get("status") or (data.get("patch") or {}).get("status")
            if tid and status in ("completed", "failed", "cancelled", "killed", "error"):
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
            log.exception("reader loop ended")

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
                self._awaiting_user_segment = True
                await self._client.query(prompt)
                # Wait for the turn to end — but never hang forever. If the stream goes
                # totally silent (no message at all) for STUCK_SECS, give up and release
                # the turn so the dispatcher isn't wedged. (Long foreground tools still
                # produce stream activity / cap well under this.)
                STUCK_SECS = 900
                loop = asyncio.get_event_loop()
                while not self._segment_done.is_set():
                    try:
                        await asyncio.wait_for(self._segment_done.wait(), 30)
                    except asyncio.TimeoutError:
                        idle = loop.time() - (self.last_activity or loop.time())
                        if idle >= STUCK_SECS:
                            log.warning("ask: no stream activity for %.0fs and no result — "
                                        "releasing the turn (assumed stuck)", idle)
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

    async def kill(self) -> list[int]:
        """Hard-kill (SIGKILL) the Claude CLI subprocess. The next turn reconnects,
        resuming the session. Deliberately does NOT take the lock, so it works even
        when a turn is stuck."""
        killed = sigkill_claude_subtree()
        client = self._client
        self._stop_reader()
        self._reset_live_state()
        self._client = None  # force a reconnect on the next ask()
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
