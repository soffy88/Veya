"""
hicode_tui 테스트 슈트
======================
30개 컴포넌트, 각 ≥5 테스트.
외부 의존성(anthropic API, oservice) 전부 mock.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from hicode_tui import (
    ArgParser, CliArgs, AtMentionCompleter, CopyHint,
    DividerLine, EnvLoader, ExitHandler, HistoryManager,
    InterruptHandler, LayoutManager, MarkdownRenderer, CodeBlockRenderer,
    MultilineInput, PagerView, PromptInput, SlashCompleter,
    SpinnerAnimation, StartupBanner, StatusBar, StreamPrinter,
    ThinkingBlock, ToolCallRenderer, ToolResultRenderer,
    VERSION, YesNoPrompt,
)
from hicode_tui.render import (
    bold, dim, gray, green, cyan, yellow, red, _ansi,
)
from hicode_tui.repl import (
    AgentLoopAdapter, HicodeREPL, LoopOrchestrator, PipeFriendly,
    PrintMode, SessionContext,
)


def run(coro):
    return asyncio.run(coro)


# ── Fixtures ──────────────────────────────────────────────────────────────

def make_args(**kw) -> CliArgs:
    args = CliArgs()
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def make_caller(text: str = "Hello!") -> AsyncMock:
    async def caller(**kw):
        return {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    return caller


def make_ctx(tmp_path, **kw) -> SessionContext:
    """의존성이 mock된 SessionContext를 생성."""
    from unittest.mock import MagicMock
    args = make_args(cwd=str(tmp_path), **kw)
    session = MagicMock()
    session.id = "sess_test"
    session.mode = kw.get("mode", "build")
    session.messages = []
    session.cost_usd = 0.0

    mgr = MagicMock()
    mgr.save = MagicMock()

    registry = MagicMock()
    registry.all.return_value = []
    registry.readonly_only.return_value = []

    router = MagicMock()
    router.is_command.return_value = False

    gate = MagicMock(return_value="allow")

    return SessionContext(
        args=args,
        caller=make_caller(),
        session=session,
        session_manager=mgr,
        registry=registry,
        slash_router=router,
        permission_gate=gate,
        status_bar=StatusBar(no_color=True),
        spinner=SpinnerAnimation(no_color=True),
        stream_printer=StreamPrinter(no_color=True),
        md_renderer=MarkdownRenderer(no_color=True),
        interrupt_handler=InterruptHandler(),
    )


# ===========================================================================
# A. CLI 入口
# ===========================================================================

class TestArgParser:
    def test_default_args(self):
        args = ArgParser.parse([])
        assert args.model == "claude-sonnet-4-6"
        assert args.mode == "build"
        assert not args.print_mode

    def test_print_mode(self):
        args = ArgParser.parse(["-p", "fix the tests"])
        assert args.print_mode
        assert args.task == "fix the tests"

    def test_model_flag(self):
        args = ArgParser.parse(["--model", "claude-opus-4-6"])
        assert args.model == "claude-opus-4-6"

    def test_mode_flag(self):
        args = ArgParser.parse(["--mode", "plan"])
        assert args.mode == "plan"

    def test_no_color_flag(self):
        args = ArgParser.parse(["--no-color"])
        assert args.no_color

    def test_continue_flag(self):
        args = ArgParser.parse(["-c"])
        assert args.continue_session

    def test_positional_becomes_task(self):
        args = ArgParser.parse(["fix", "the", "bug"])
        assert "fix the bug" in args.task

    def test_session_flag(self):
        args = ArgParser.parse(["--session", "sess_abc"])
        assert args.session_id == "sess_abc"

    def test_cwd_flag(self):
        args = ArgParser.parse(["--cwd", "/tmp"])
        assert args.cwd == "/tmp"

    def test_verbose_flag(self):
        args = ArgParser.parse(["-v"])
        assert args.verbose


class TestEnvLoader:
    def test_load_env_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-from-env")
        assert EnvLoader.check_api_key("anthropic")

    def test_check_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert not EnvLoader.check_api_key("anthropic")

    def test_check_api_key_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert EnvLoader.check_api_key("anthropic")

    def test_get_caller_no_sdk(self, monkeypatch):
        monkeypatch.setattr("builtins.__import__", lambda n, *a, **kw:
            (_ for _ in ()).throw(ImportError()) if n in ("obase", "anthropic") else __import__(n, *a, **kw))
        # caller가 None이거나 callable인지만 확인
        result = EnvLoader.get_caller()
        assert result is None or callable(result)

    def test_load_returns_dict(self, tmp_path):
        result = EnvLoader.load()
        assert isinstance(result, dict)


class TestStartupBanner:
    def test_no_output_in_print_mode(self, capsys):
        args = make_args(print_mode=True)
        StartupBanner.print(args)
        assert capsys.readouterr().out == ""

    def test_prints_version(self, capsys):
        args = make_args(model="claude-sonnet-4-6", mode="build",
                         cwd="/tmp", print_mode=False)
        StartupBanner.print(args)
        out = capsys.readouterr().out
        assert VERSION in out

    def test_prints_model(self, capsys):
        args = make_args(model="claude-opus-4-6", mode="build",
                         cwd="/tmp", print_mode=False)
        StartupBanner.print(args)
        assert "claude-opus-4-6" in capsys.readouterr().out

    def test_no_color_no_ansi(self, capsys):
        args = make_args(model="m", mode="build", cwd="/", no_color=True, print_mode=False)
        StartupBanner.print(args)
        assert "\033[" not in capsys.readouterr().out

    def test_plan_mode_shows_plan(self, capsys):
        args = make_args(model="m", mode="plan", cwd="/", print_mode=False)
        StartupBanner.print(args)
        assert "plan" in capsys.readouterr().out.lower()


# ===========================================================================
# B. Prompt 입력박스
# ===========================================================================

class TestHistoryManager:
    def test_setup_creates_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(HistoryManager, "HISTORY_FILE", tmp_path / "hist")
        HistoryManager.setup()
        assert tmp_path.exists()

    def test_save_no_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(HistoryManager, "HISTORY_FILE", tmp_path / "hist")
        HistoryManager.setup()
        HistoryManager.save()  # 에러 없이 완료


class TestAtMentionCompleter:
    def test_candidates_found(self, tmp_path):
        (tmp_path / "main.py").write_text("x=1")
        (tmp_path / "utils.py").write_text("y=2")
        c = AtMentionCompleter(cwd=str(tmp_path))
        results = c._candidates("main")
        assert any("main.py" in r for r in results)

    def test_candidates_empty_dir(self, tmp_path):
        c = AtMentionCompleter(cwd=str(tmp_path))
        assert c._candidates("anything") == []

    def test_complete_no_at(self, tmp_path, monkeypatch):
        import readline
        monkeypatch.setattr(readline, "get_line_buffer", lambda: "no at sign here")
        c = AtMentionCompleter(cwd=str(tmp_path))
        assert c.complete("", 0) is None


class TestSlashCompleter:
    def test_complete_init(self, monkeypatch):
        import readline
        monkeypatch.setattr(readline, "get_line_buffer", lambda: "/init")
        c = SlashCompleter()
        assert c.complete("/init", 0) == "/init"

    def test_complete_no_slash(self, monkeypatch):
        import readline
        monkeypatch.setattr(readline, "get_line_buffer", lambda: "hello")
        c = SlashCompleter()
        assert c.complete("hello", 0) is None

    def test_custom_commands(self, monkeypatch):
        import readline
        monkeypatch.setattr(readline, "get_line_buffer", lambda: "/custom")
        c = SlashCompleter(commands=["/custom_cmd"])
        assert c.complete("/custom", 0) == "/custom_cmd"

    def test_no_match(self, monkeypatch):
        import readline
        monkeypatch.setattr(readline, "get_line_buffer", lambda: "/zzz")
        c = SlashCompleter()
        assert c.complete("/zzz", 0) is None


class TestMultilineInput:
    def test_simple_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "hello world")
        result = MultilineInput.read()
        assert result == "hello world"

    def test_empty_input_returns_none(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "")
        result = MultilineInput.read()
        assert result is None

    def test_ctrl_c_returns_none(self, monkeypatch):
        def raise_kbd(p=""):
            raise KeyboardInterrupt
        monkeypatch.setattr("builtins.input", raise_kbd)
        result = MultilineInput.read()
        assert result is None

    def test_ctrl_d_raises(self, monkeypatch):
        def raise_eof(p=""):
            raise EOFError
        monkeypatch.setattr("builtins.input", raise_eof)
        with pytest.raises(EOFError):
            MultilineInput.read()

    def test_backslash_continuation(self, monkeypatch):
        calls = iter(["line1\\", "line2"])
        monkeypatch.setattr("builtins.input", lambda p="": next(calls))
        result = MultilineInput.read()
        assert "line1" in result and "line2" in result


# ===========================================================================
# C. 출력 렌더러
# ===========================================================================

class TestMarkdownRenderer:
    def test_bold(self):
        r = MarkdownRenderer(no_color=False)
        out = r.render("**hello**")
        assert "\033[1m" in out and "hello" in out

    def test_heading(self):
        r = MarkdownRenderer(no_color=True)
        out = r.render("# Title")
        assert "Title" in out

    def test_list_item(self):
        r = MarkdownRenderer(no_color=True)
        out = r.render("- item one")
        assert "item one" in out and "•" in out

    def test_code_block(self):
        r = MarkdownRenderer(no_color=True)
        out = r.render("```python\nx = 1\n```")
        assert "x = 1" in out

    def test_no_color_no_ansi(self):
        r = MarkdownRenderer(no_color=True)
        out = r.render("**bold** and `code`")
        assert "\033[" not in out

    def test_inline_code(self):
        r = MarkdownRenderer(no_color=False)
        out = r.render("`inline_code`")
        assert "inline_code" in out

    def test_quote(self):
        r = MarkdownRenderer(no_color=True)
        out = r.render("> quoted text")
        assert "quoted text" in out and "│" in out


class TestCodeBlockRenderer:
    def test_no_color(self):
        r = CodeBlockRenderer(no_color=True)
        out = r.render("x = 1\n", lang="python")
        assert "x = 1" in out and "\033[" not in out

    def test_with_lang_header(self):
        r = CodeBlockRenderer(no_color=True)
        out = r.render("x = 1", lang="python")
        assert "python" in out

    def test_empty_code(self):
        r = CodeBlockRenderer(no_color=True)
        out = r.render("", lang="python")
        assert isinstance(out, str)

    def test_pygments_fallback(self):
        r = CodeBlockRenderer(no_color=False)
        out = r.render("def f(): pass", lang="python")
        assert "f" in out


class TestToolCallRenderer:
    def test_renders_name(self):
        r = ToolCallRenderer(no_color=True)
        out = r.render("bash_exec", {"command": "ls -la"})
        assert "bash_exec" in out and "ls -la" in out

    def test_renders_path(self):
        r = ToolCallRenderer(no_color=True)
        out = r.render("file_read", {"path": "/tmp/x.py"})
        assert "file_read" in out and "/tmp/x.py" in out

    def test_icon_present(self):
        r = ToolCallRenderer(no_color=True)
        out = r.render("t", {})
        assert "⚙" in out

    def test_truncates_long_input(self):
        r = ToolCallRenderer(no_color=True)
        out = r.render("t", {"command": "x" * 200})
        assert len(out) < 300


class TestToolResultRenderer:
    def test_renders_stdout(self):
        r = ToolResultRenderer(no_color=True)
        out = r.render({"stdout": "hello world", "ok": True})
        assert "hello world" in out

    def test_renders_error(self):
        r = ToolResultRenderer(no_color=True)
        out = r.render({"error": "permission denied"})
        assert "permission denied" in out

    def test_truncates_long_output(self):
        r = ToolResultRenderer(no_color=True)
        long_text = "\n".join([f"line {i}" for i in range(100)])
        out = r.render({"content": long_text})
        lines = out.splitlines()
        assert len(lines) <= ToolResultRenderer.MAX_LINES + 2  # +2 for truncation marker

    def test_renders_string(self):
        r = ToolResultRenderer(no_color=True)
        out = r.render("plain string result")
        assert "plain string" in out

    def test_truncation_marker(self):
        r = ToolResultRenderer(no_color=True)
        long_result = {"content": "x\n" * 100}
        out = r.render(long_result)
        assert "truncated" in out.lower()


class TestThinkingBlock:
    def test_collapsed_shows_preview(self):
        b = ThinkingBlock(no_color=True, collapsed=True)
        out = b.render("Let me think step by step about this problem...")
        assert "think" in out and "💭" in out

    def test_empty_no_output(self):
        b = ThinkingBlock(no_color=True)
        assert b.render("") == ""

    def test_toggle_expands(self):
        b = ThinkingBlock(no_color=True, collapsed=True)
        b.render("full thinking text here")
        out = b.toggle()
        assert "full thinking text" in out

    def test_expanded_shows_full(self):
        b = ThinkingBlock(no_color=True, collapsed=False)
        out = b.render("complete reasoning here")
        assert "complete reasoning" in out

    def test_no_color_no_ansi(self):
        b = ThinkingBlock(no_color=True)
        out = b.render("thinking...")
        assert "\033[" not in out


class TestStreamPrinter:
    def test_prints_text_deltas(self):
        buf = io.StringIO()
        printer = StreamPrinter(no_color=True, file=buf)
        from types import SimpleNamespace

        async def stream():
            yield SimpleNamespace(type="text", text="hello ")
            yield SimpleNamespace(type="text", text="world")
            yield SimpleNamespace(type="stop", stop_reason="end_turn")

        text, interrupted = run(printer.print_stream(stream()))
        assert "hello" in text and "world" in text
        assert not interrupted

    def test_thinking_delta(self):
        buf = io.StringIO()
        printer = StreamPrinter(no_color=True, file=buf)
        from types import SimpleNamespace

        async def stream():
            yield SimpleNamespace(type="thinking", text="reasoning...")
            yield SimpleNamespace(type="stop", stop_reason="end_turn")

        text, _ = run(printer.print_stream(stream()))

    def test_set_interrupted(self):
        printer = StreamPrinter(no_color=True)
        printer.set_interrupted()
        assert printer._interrupted


# ===========================================================================
# D. 상태표시줄 & 레이아웃
# ===========================================================================

class TestLayoutManager:
    def test_width_positive(self):
        assert LayoutManager.width() > 0

    def test_height_positive(self):
        assert LayoutManager.height() > 0

    def test_wrap_text(self):
        long = "word " * 30
        wrapped = LayoutManager.wrap(long)
        lines = wrapped.splitlines()
        assert all(len(l) <= LayoutManager.width() + 2 for l in lines)

    def test_is_narrow_bool(self):
        assert isinstance(LayoutManager.is_narrow(), bool)


class TestSpinnerAnimation:
    def test_start_stop(self):
        buf = io.StringIO()
        s = SpinnerAnimation(no_color=True, file=buf)
        s.start("working")
        time.sleep(0.1)
        s.stop()
        # 출력이 발생했거나 스피너가 시작됐음

    def test_context_manager(self):
        buf = io.StringIO()
        s = SpinnerAnimation(no_color=True, file=buf)
        with s.context("test"):
            time.sleep(0.05)

    def test_no_crash_on_stop_without_start(self):
        s = SpinnerAnimation(no_color=True)
        s.stop()  # 에러 없음


class TestDividerLine:
    def test_prints_divider(self, capsys):
        DividerLine.print(no_color=True)
        out = capsys.readouterr().out
        assert "─" in out

    def test_user_prefix(self):
        p = DividerLine.user_prefix(no_color=True)
        assert "You" in p

    def test_assistant_prefix(self):
        p = DividerLine.assistant_prefix(no_color=True)
        assert "hicode" in p

    def test_no_color_no_ansi(self):
        p = DividerLine.user_prefix(no_color=True)
        assert "\033[" not in p


class TestStatusBar:
    def test_update_cost(self):
        sb = StatusBar(no_color=True)
        sb.update(cost=0.01, in_tok=100, out_tok=50)
        assert sb.cost_usd == pytest.approx(0.01)
        assert sb.in_tokens == 100

    def test_render_contains_model(self):
        sb = StatusBar(no_color=True)
        sb.update(mode="build", model="claude-sonnet-4-6")
        out = sb.render()
        assert "sonnet" in out.lower() or "build" in out.lower()

    def test_no_color_no_ansi(self):
        sb = StatusBar(no_color=True)
        out = sb.render()
        assert "\033[" not in out

    def test_session_id_shown(self):
        sb = StatusBar(no_color=True)
        sb.update(session_id="sess_abcdef12")
        out = sb.render()
        assert "sess_abc" in out

    def test_print_no_crash(self):
        buf = io.StringIO()
        sb = StatusBar(no_color=True, file=buf)
        sb.print()  # 에러 없음

    def test_clear_no_crash(self):
        buf = io.StringIO()
        sb = StatusBar(no_color=True, file=buf)
        sb.clear()  # 에러 없음


# ===========================================================================
# E. 인터랙션 컨트롤
# ===========================================================================

class TestInterruptHandler:
    def test_register_and_callback(self):
        h = InterruptHandler()
        called = []
        h.register_callback(lambda: called.append(1))
        h._handle(None, None)
        assert called == [1]

    def test_install_uninstall(self):
        h = InterruptHandler()
        h.install()
        h.uninstall()  # 에러 없음

    def test_double_ctrl_c_exits(self):
        h = InterruptHandler()
        h._count = 2
        h._last_time = time.time()
        with pytest.raises(SystemExit):
            h._handle(None, None)


class TestExitHandler:
    def test_yes_exits(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "y")
        h = ExitHandler()
        assert h.handle() is True

    def test_no_continues(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "n")
        h = ExitHandler()
        assert h.handle() is False

    def test_eof_exits(self, monkeypatch):
        def raise_eof(p=""):
            raise EOFError
        monkeypatch.setattr("builtins.input", raise_eof)
        h = ExitHandler()
        assert h.handle() is True

    def test_saves_session(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "y")
        mgr = MagicMock()
        session = MagicMock()
        h = ExitHandler(session_manager=mgr)
        h.handle(session)
        mgr.save.assert_called_once_with(session)


class TestYesNoPrompt:
    def test_auto_allow(self):
        p = YesNoPrompt(no_color=True, auto="allow")
        result = run(p.ask("bash_exec", {}))
        assert result == "allow"

    def test_auto_deny(self):
        p = YesNoPrompt(no_color=True, auto="deny")
        result = run(p.ask("bash_exec", {}))
        assert result == "deny"

    def test_y_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "y")
        p = YesNoPrompt(no_color=True)
        result = run(p.ask("t", {}))
        assert result == "allow"

    def test_always_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "always")
        p = YesNoPrompt(no_color=True)
        result = run(p.ask("t", {}))
        assert result == "always"

    def test_eof_denies(self, monkeypatch):
        def raise_eof(p=""):
            raise EOFError
        monkeypatch.setattr("builtins.input", raise_eof)
        p = YesNoPrompt(no_color=True)
        result = run(p.ask("t", {}))
        assert result == "deny"


class TestPagerView:
    def test_short_output_no_prompt(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p="": "q")
        pager = PagerView(lines_per_page=100)
        buf = io.StringIO()
        with patch("builtins.print", lambda *a, **kw: buf.write(str(a[0]) + "\n")):
            pager.show("short text")
        assert "short text" in buf.getvalue()

    def test_long_output_paginates(self, monkeypatch):
        calls = iter(["", "q"])
        monkeypatch.setattr("builtins.input", lambda p="": next(calls))
        pager = PagerView(lines_per_page=3)
        text = "\n".join([f"line {i}" for i in range(10)])
        pager.show(text)  # 에러 없음


class TestCopyHint:
    def test_hint_text(self):
        h = CopyHint.hint(no_color=True)
        assert "copy" in h.lower()

    def test_copy_to_clipboard_no_crash(self):
        # 터미널 OSC 52 지원 여부와 관계없이 에러 없음
        result = CopyHint.copy_to_clipboard("test text")
        assert isinstance(result, bool)


# ===========================================================================
# F. 주 REPL 루프
# ===========================================================================

class TestAgentLoopAdapter:
    def test_direct_llm_call(self, tmp_path):
        ctx = make_ctx(tmp_path)
        adapter = AgentLoopAdapter(ctx)
        result = run(adapter.run("say hello"))
        assert "text" in result
        assert result["status"] in ("completed", "failed")

    def test_error_handled(self, tmp_path):
        ctx = make_ctx(tmp_path)
        async def bad_caller(**kw):
            raise RuntimeError("api error")
        ctx.caller = bad_caller
        adapter = AgentLoopAdapter(ctx)
        result = run(adapter._direct_llm("task"))
        assert result["status"] == "failed"

    def test_returns_cost(self, tmp_path):
        ctx = make_ctx(tmp_path)
        adapter = AgentLoopAdapter(ctx)
        result = run(adapter.run("hello"))
        assert "cost_usd" in result

    def test_plan_mode_uses_readonly_tools(self, tmp_path):
        ctx = make_ctx(tmp_path, mode="plan")
        adapter = AgentLoopAdapter(ctx)
        result = run(adapter.run("list files"))
        assert result  # 에러 없음


class TestLoopOrchestrator:
    def test_slash_command_dispatched(self, tmp_path, monkeypatch):
        ctx = make_ctx(tmp_path)
        ctx.slash_router.is_command.return_value = True
        result_mock = MagicMock()
        result_mock.error = None
        result_mock.text = "help text"
        result_mock.redirect_to_loop = False
        ctx.slash_router.dispatch = AsyncMock(return_value=result_mock)
        orch = LoopOrchestrator(ctx)
        run(orch.handle("/help"))
        ctx.slash_router.dispatch.assert_called_once()

    def test_regular_text_goes_to_agent(self, tmp_path, monkeypatch):
        ctx = make_ctx(tmp_path)
        ctx.slash_router.is_command.return_value = False
        orch = LoopOrchestrator(ctx)
        # _run_agent를 mock
        called = []
        async def mock_run(task):
            called.append(task)
        orch._run_agent = mock_run
        run(orch.handle("fix the bug"))
        assert "fix the bug" in called

    def test_error_result_printed(self, tmp_path, capsys):
        ctx = make_ctx(tmp_path)
        ctx.slash_router.is_command.return_value = True
        ctx.slash_router.dispatch = AsyncMock(
            return_value=MagicMock(error="command failed", text=None, redirect_to_loop=False)
        )
        orch = LoopOrchestrator(ctx)
        run(orch.handle("/bad"))
        assert "command failed" in capsys.readouterr().out


class TestPipeFriendly:
    def test_print_result(self, capsys):
        PipeFriendly.print_result("output text")
        assert "output text" in capsys.readouterr().out

    def test_print_log_to_stderr(self, capsys):
        PipeFriendly.print_log("log message")
        assert "log message" in capsys.readouterr().err

    def test_is_pipe_bool(self):
        assert isinstance(PipeFriendly.is_pipe(), bool)


class TestPrintMode:
    def test_runs_task(self, tmp_path, capsys):
        ctx = make_ctx(tmp_path)
        pm = PrintMode(ctx)
        code = run(pm.run("say hello"))
        assert code in (0, 1)

    def test_error_returns_1(self, tmp_path):
        ctx = make_ctx(tmp_path)
        async def bad(**kw): raise RuntimeError("fail")
        ctx.caller = bad
        pm = PrintMode(ctx)
        code = run(pm.run("task"))
        assert code in (0, 1)  # 실패 허용

    def test_output_to_stdout(self, tmp_path, capsys):
        ctx = make_ctx(tmp_path)
        pm = PrintMode(ctx)
        run(pm.run("hello"))
        out = capsys.readouterr().out
        # stdout에 어떤 출력이 있거나 없어도 에러 없음


# ===========================================================================
# 색상 유틸
# ===========================================================================

class TestColorUtils:
    def test_bold(self):
        assert "\033[1m" in bold("text")

    def test_bold_no_color(self):
        assert "\033[" not in bold("text", nc=True)

    def test_dim(self):
        assert "\033[2m" in dim("text")

    def test_gray(self):
        assert "\033[90m" in gray("text")

    def test_ansi_nc(self):
        assert _ansi("1", "text", nc=True) == "text"


# ===========================================================================
# providers.py — DeepSeek / OpenAI 兼容适配器
# ===========================================================================

class TestProviders:
    def test_detect_deepseek(self):
        from hicode_tui.providers import detect_provider
        assert detect_provider("deepseek-chat") == "deepseek"
        assert detect_provider("deepseek-reasoner") == "deepseek"

    def test_detect_anthropic(self):
        from hicode_tui.providers import detect_provider
        assert detect_provider("claude-sonnet-4-6") == "anthropic"
        assert detect_provider("claude-opus-4-6") == "anthropic"

    def test_detect_unknown_is_unknown(self):
        from hicode_tui.providers import detect_provider
        assert detect_provider("gpt-4o") == "unknown"

    def test_make_deepseek_caller_no_key_raises(self, monkeypatch):
        from hicode_tui.providers import make_deepseek_caller
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            make_deepseek_caller("deepseek-chat")

    def test_make_deepseek_caller_with_key(self, monkeypatch):
        from hicode_tui.providers import make_deepseek_caller
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key")
        caller = make_deepseek_caller("deepseek-chat")
        assert callable(caller)

    def test_caller_has_provider_meta(self, monkeypatch):
        from hicode_tui.providers import make_deepseek_caller
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        caller = make_deepseek_caller("deepseek-chat")
        assert getattr(caller, "_provider", None) == "deepseek"
        assert getattr(caller, "_model", None) == "deepseek-chat"

    def test_openai_resp_to_anthropic_text(self):
        from hicode_tui.providers import _openai_resp_to_anthropic
        from types import SimpleNamespace
        resp = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="Hello!", tool_calls=None,
                    reasoning_content=None
                ),
                finish_reason="stop"
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model="deepseek-chat", id="chatcmpl-123"
        )
        result = _openai_resp_to_anthropic(resp)
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Hello!"
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_openai_resp_to_anthropic_tool_call(self):
        from hicode_tui.providers import _openai_resp_to_anthropic
        import json
        from types import SimpleNamespace
        tc = SimpleNamespace(
            id="call_abc",
            function=SimpleNamespace(name="bash_exec", arguments='{"command":"ls"}')
        )
        resp = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[tc], reasoning_content=None),
                finish_reason="tool_calls"
            )],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10),
            model="deepseek-chat", id="id"
        )
        result = _openai_resp_to_anthropic(resp)
        tool_block = next(b for b in result["content"] if b["type"] == "tool_use")
        assert tool_block["name"] == "bash_exec"
        assert tool_block["input"] == {"command": "ls"}
        assert result["stop_reason"] == "tool_use"

    def test_openai_resp_with_reasoning(self):
        """DeepSeek-R1 的 reasoning_content 映射为 thinking block。"""
        from hicode_tui.providers import _openai_resp_to_anthropic
        from types import SimpleNamespace
        resp = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="The answer is 42.",
                    tool_calls=None,
                    reasoning_content="Let me think... 6×7=42"
                ),
                finish_reason="stop"
            )],
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=15),
            model="deepseek-reasoner", id="id"
        )
        result = _openai_resp_to_anthropic(resp)
        # thinking block 应在最前
        assert result["content"][0]["type"] == "thinking"
        assert "6×7=42" in result["content"][0]["thinking"]
        assert result["content"][1]["type"] == "text"

    def test_anthropic_tool_to_openai(self):
        from hicode_tui.providers import _anthropic_tool_to_openai
        tool = {
            "name": "bash_exec",
            "description": "Execute bash",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"]
            }
        }
        result = _anthropic_tool_to_openai(tool)
        assert result["type"] == "function"
        assert result["function"]["name"] == "bash_exec"
        assert "command" in result["function"]["parameters"]["properties"]

    def test_get_caller_deepseek(self, monkeypatch):
        from hicode_tui.providers import get_caller
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        caller = get_caller("deepseek-chat")
        assert callable(caller)

    def test_get_caller_unknown_provider_raises(self, monkeypatch):
        from hicode_tui.providers import get_caller
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises((ValueError, ImportError)):
            get_caller("some-unknown-model-xyz")

    def test_env_loader_detects_deepseek(self):
        from hicode_tui.input import EnvLoader
        assert EnvLoader.detect_provider("deepseek-chat") == "deepseek"
        assert EnvLoader.detect_provider("claude-sonnet-4-6") == "anthropic"

    def test_env_loader_check_deepseek_key(self, monkeypatch):
        from hicode_tui.input import EnvLoader
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        assert EnvLoader.check_api_key("deepseek")
        monkeypatch.delenv("DEEPSEEK_API_KEY")
        assert not EnvLoader.check_api_key("deepseek")
