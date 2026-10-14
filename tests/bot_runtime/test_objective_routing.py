from types import SimpleNamespace

from server.bot_runtime.execution import (
    looks_like_shell_command,
    resolve_execution_route,
    select_admission_executor,
)


def test_looks_like_shell_command():
    # True for intentional commands
    assert looks_like_shell_command("echo hello")
    assert looks_like_shell_command("false")
    assert looks_like_shell_command("true")
    assert looks_like_shell_command("ls -la")
    assert looks_like_shell_command("pytest tests/")
    assert looks_like_shell_command("cat README.md")

    # False for prose / natural language / Chinese
    assert not looks_like_shell_command("帮我写一个爬虫")
    assert not looks_like_shell_command("实现用户登录功能")
    assert not looks_like_shell_command("Please fix the bug in auth")
    assert not looks_like_shell_command("Write a python script to fetch news")
    assert not looks_like_shell_command("ship it")
    assert not looks_like_shell_command("survive restart")
    assert not looks_like_shell_command("keep working")
    assert not looks_like_shell_command("")
    assert not looks_like_shell_command("   ")


def test_resolve_execution_route_prose():
    route = resolve_execution_route("Write a script")
    assert not route.native
    assert route.executor != "local2"
    assert route.executor != ""


def test_resolve_execution_route_command():
    route = resolve_execution_route("echo 123")
    assert route.native
    assert route.executor == "local2"


def test_select_admission_executor_fallback(monkeypatch):
    class FakeRegistry:
        def ordered_ids(self):
            return ("codex", "opencode", "pi")

        def snapshot(self):
            return {
                "codex": SimpleNamespace(selectable=False),
                "opencode": SimpleNamespace(selectable=True),
                "pi": SimpleNamespace(selectable=True),
            }

    monkeypatch.setattr(
        "server.bot_runtime.execution.get_executor_registry", lambda: FakeRegistry()
    )
    chosen = select_admission_executor("codex")
    # codex is not selectable -> falls back to opencode
    assert chosen == "opencode"


def test_select_admission_executor_exhausted(monkeypatch):
    class EmptyRegistry:
        def ordered_ids(self):
            return ("codex",)

        def snapshot(self):
            return {
                "codex": SimpleNamespace(selectable=False),
            }

    monkeypatch.setattr(
        "server.bot_runtime.execution.get_executor_registry", lambda: EmptyRegistry()
    )
    chosen = select_admission_executor("codex")
    assert chosen == ""
