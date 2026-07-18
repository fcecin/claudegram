"""Multi-session multiplexing: registry, per-session routing (concurrent), badges,
teardown. No Telegram, no Claude — routing is proven by a stubbed dispatch recorder."""

import asyncio

import bot
from tests.fakes import FakeApp, make_fake_session


def test_default_is_single_session_untagged():
    reg = bot.registry
    assert list(reg.sessions) == ["claude"]
    assert reg.multiplexing() is False
    assert reg.badge(reg.current()) == ""          # progressive disclosure: no tag
    assert bot.controller is reg.current().controller


def test_roster_is_assembled_from_disk_with_config_icons():
    # The selectable roster is scanned from bots/*/, default 'claude' first, internal bots (jack)
    # excluded. Icons come from each bot's config, not from code.
    sel = bot.selectable_bots()
    assert sel[0] == "claude"                     # default pinned first
    assert set(sel) == {"claude", "blu", "gil", "ava", "ily", "max", "gol", "nyx", "sno"}
    assert bot.NOSTALL_BOT not in sel             # internal bot is not selectable
    assert bot.bot_icon("gil") == "🟢" and bot.bot_icon("nyx") == "⚫"
    assert bot.Session("gil").emoji == "🟢"        # Session badge derives from config icon


async def test_select_creates_switches_and_turns_on_tagging():
    reg = bot.registry
    bot._app = FakeApp()
    s = bot.select_session("gil")
    assert reg.multiplexing() is True
    assert s.name == "gil" and s.emoji == "🟢"
    assert reg.badge(s) == "🟢 gil · "
    assert reg.badge(reg.get("claude")) == "🟠 claude · "   # tags on for ALL now
    assert reg.current_name == "gil"
    assert bot.controller is s.controller                   # module global tracks current


def test_unknown_session_name_rejected():
    assert not bot.registry.known("zzz")
    assert all(bot.registry.known(n) for n in
               ("blu", "gil", "ava", "ily", "max", "gol", "nyx", "sno"))


async def test_routing_is_per_session_and_concurrent():
    reg = bot.registry
    bot._app = FakeApp()
    calls = []
    orig = bot.dispatch_to_claude

    async def rec(context, session, chat_id, reply_to, user_text, source,
                  raw=False, header="", voiceback=False):
        calls.append((session.name, user_text))
    bot.dispatch_to_claude = rec
    try:
        bot._activate_session(reg.current())          # claude worker
        bot.select_session("gil")                     # gil worker (activated on create)
        bot.enqueue_for_claude(reg.get("gil"), 1, None, "build", "text")
        bot.enqueue_for_claude(reg.get("claude"), 1, None, "lint", "text")
        await asyncio.sleep(0.25)                      # debounce is 0.02 in tests
        assert sorted(calls) == [("claude", "lint"), ("gil", "build")], calls
    finally:
        bot.dispatch_to_claude = orig


async def test_no_more_work_flag_is_per_session():
    bot._app = FakeApp()
    bot.select_session("gil")
    reg = bot.registry
    bot.set_no_more_work(reg.get("gil"), True)
    assert bot.is_no_more_work(reg.get("gil")) is True
    assert bot.is_no_more_work(reg.get("claude")) is False   # NOT global — no bleed


async def test_end_session_tears_down_and_untags():
    reg = bot.registry
    bot._app = FakeApp()
    bot.select_session("gil")
    assert reg.multiplexing() is True
    status = await bot.end_session("gil")
    assert "gil" in status
    assert reg.multiplexing() is False
    assert reg.current_name == "claude"                 # fell back to default
    assert reg.badge(reg.get("claude")) == ""           # tagging off again


async def test_cannot_end_default_session():
    msg = await bot.end_session("claude")
    assert "can't" in msg.lower()
    assert "claude" in bot.registry.sessions


async def test_end_session_teardown_and_default_guard():
    # the mechanism auto-end uses: tear down a named session, never the default.
    bot._app = FakeApp()
    bot.registry.sessions["gil"] = make_fake_session("gil")
    await bot.end_session("gil")
    assert "gil" not in bot.registry.sessions
    assert "default" in (await bot.end_session("claude")).lower()
    assert "no live session" in (await bot.end_session("zzz")).lower()


async def test_bot_lock_kills_every_session():
    # `bot lock` is PANIC: under multiplexing "the" Claude is N of them. It must kill EVERY
    # session's controller (like the intrusion hard-lock), not just the current one.
    import types
    from tests.fakes import FakeBot, clear_flags
    reg = bot.registry
    reg.sessions["claude"] = make_fake_session("claude")
    reg.sessions["gil"] = make_fake_session("gil")
    fb = FakeBot()
    try:
        assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb), 1, None, "bot lock")
        assert reg.sessions["claude"].controller.kill_calls == 1
        assert reg.sessions["gil"].controller.kill_calls == 1     # the sibling died too
        assert bot.is_blocked() is True
        assert any("Every Claude session killed" in s for s in fb.sent), fb.sent
    finally:
        clear_flags()


async def test_batches_never_mix_chats():
    # A drain takes ONE chat's burst per turn: a guest's and the master's messages must
    # never fuse into one prompt, and each answer must go back to its own chat.
    reg = bot.registry
    bot._app = FakeApp()
    calls = []
    orig = bot.dispatch_to_claude

    async def rec(context, session, chat_id, reply_to, user_text, source,
                  raw=False, header="", voiceback=False):
        calls.append((chat_id, user_text))
    bot.dispatch_to_claude = rec
    try:
        bot._activate_session(reg.current())
        s = reg.current()
        bot.enqueue_for_claude(s, 1, None, "a1", "text")   # master
        bot.enqueue_for_claude(s, 2, None, "b1", "text")   # guest, same debounce window
        bot.enqueue_for_claude(s, 1, None, "a2", "text")   # master again
        await asyncio.sleep(0.4)                            # debounce is 0.02 in tests
        assert (1, "a1\n\na2") in calls, calls              # master's burst still merges
        assert (2, "b1") in calls, calls                    # guest answered in THEIR chat
        assert not any("a1" in t and "b1" in t for _, t in calls)   # never fused
    finally:
        bot.dispatch_to_claude = orig


async def test_end_session_teardown_tasks_hold_strong_refs():
    # asyncio keeps only weak refs to tasks: a bare create_task(end_session(...)) can be
    # GC'd mid-teardown. Both call sites must go through _spawn (pinned by source scan),
    # and the nostall-off path must actually tear the guard down.
    import types
    from tests.fakes import FakeBot
    src = (bot.HERE / "bot.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(end_session" not in src   # only _spawn(end_session(...))
    bot.set_nostall(True)
    bot.registry.sessions["jack"] = make_fake_session("jack")
    fb = FakeBot()
    assert await bot.maybe_handle_bot_command(types.SimpleNamespace(bot=fb), 1, None, "bot nostall off")
    assert any(t.get_name() == "end_session[jack]" for t in bot._bg_tasks)  # strong ref held
    await asyncio.sleep(0.05)
    assert "jack" not in bot.registry.sessions            # ...and the teardown really ran
