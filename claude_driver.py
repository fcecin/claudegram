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
import signal
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

log = logging.getLogger("claudegram")

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


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


class ClaudeController:
    """A long-lived, multi-turn Claude Code session that resumes across restarts."""

    def __init__(self, cwd: str, session_file: str, effort_file: str | None = None,
                 cwd_file: str | None = None) -> None:
        self.session_file = Path(session_file)
        self.effort_file = Path(effort_file) if effort_file else None
        self.cwd_file = Path(cwd_file) if cwd_file else None
        self._default_cwd = str(cwd)
        self.cwd = self._load_cwd() or str(cwd)
        self.session_id = self._load_session()
        self.effort = self._load_effort()  # None => model/CLI default
        self._client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()
        self.busy = False
        self._on_system = None  # async callback(kind:str, data:dict) for the active turn

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
        """Switch Claude's working directory. Creates it if needed, persists it, and
        starts a fresh session there (sessions are per-directory). False on error."""
        p = Path(path).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        async with self._lock:
            self.cwd = str(p)
            self._save_cwd()
            self.session_id = None  # new directory => new session space
            self._save_session()
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

    # --- turns ---------------------------------------------------------------
    async def ask(self, prompt: str, on_event, on_system=None) -> None:
        async with self._lock:
            self.busy = True
            self._on_system = on_system
            try:
                await self._ensure_connected()
                await self._client.query(prompt)
                async for message in self._client.receive_response():
                    sid = getattr(message, "session_id", None)
                    if sid and sid != self.session_id:
                        self.session_id = sid
                        self._save_session()
                    await on_event(message)
            finally:
                self.busy = False
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

    async def kill(self) -> list[int]:
        """Hard-kill (SIGKILL) the Claude CLI subprocess. The next turn reconnects,
        resuming the session. Deliberately does NOT take the lock, so it works even
        when a turn is stuck."""
        killed = sigkill_claude_subtree()
        client = self._client
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
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None
            self.session_id = None
            self._save_session()
