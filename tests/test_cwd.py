import bot


def test_new_bot_uses_its_own_default_cwd():
    p = bot.HERE / "cwd.gil.path"
    if p.exists():
        p.unlink()
    bot.registry.current().controller.cwd = "/tmp/somewhere-else"
    gil = bot.Session("gil")
    assert gil.controller.get_cwd() == str(bot.CGHOME)


def test_default_session_has_a_concrete_cwd():
    assert bot.Session("claude").controller.get_cwd()


def test_default_cwd_is_install_local_work():
    # ~/cghome is gone: with no CGHOME override, the default cwd is a gitignored `work/`
    # inside the install, so each copy is self-contained.
    import os
    if os.environ.get("CGHOME"):
        return  # an explicit override is in play; the default isn't exercised
    assert bot.CGHOME == bot.HERE / "work"


async def test_cwd_relative_paths_resolve_absolute():
    import shutil
    root = "/tmp/cg-cwd-test"
    shutil.rmtree(root, ignore_errors=True)
    c = bot.ClaudeController(f"{root}/a/b", f"{root}/s.id", None, None)
    try:
        assert c.get_cwd() == f"{root}/a/b"
        assert await c.set_cwd("..")
        assert c.get_cwd() == f"{root}/a"
        assert await c.set_cwd("c/d")
        assert c.get_cwd() == f"{root}/a/c/d"
        assert await c.set_cwd(f"{root}/x")
        assert c.get_cwd() == f"{root}/x"
    finally:
        shutil.rmtree(root, ignore_errors=True)
